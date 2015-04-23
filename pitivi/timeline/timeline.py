# -*- coding: utf-8 -*-
# Pitivi video editor
#
#       pitivi/timeline/timeline.py
#
# Copyright (c) 2013, Mathieu Duponchelle <mduponchelle1@gmail.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin St, Fifth Floor,
# Boston, MA 02110-1301, USA.

import os

from gettext import gettext as _

from gi.repository import GES
from gi.repository import GLib
from gi.repository import Gdk
from gi.repository import Gst
from gi.repository import Gtk

from pitivi.utils import ui
from pitivi.autoaligner import AlignmentProgressDialog, AutoAligner
from pitivi.configure import get_ui_dir
from pitivi.dialogs.prefs import PreferencesDialog
from pitivi.settings import GlobalSettings
from pitivi.timeline.ruler import ScaleRuler
from pitivi.utils.loggable import Loggable
from pitivi.utils.timeline import Zoomable, TimelineError
from pitivi.utils.ui import alter_style_class, EXPANDED_SIZE, SPACING, CONTROL_WIDTH
from pitivi.utils.widgets import ZoomBox

from pitivi.timeline.elements import Clip
from pitivi.utils import timeline as timelineUtils
from pitivi.timeline.layer import SpacedSeparator, Layer, LayerControls

GlobalSettings.addConfigOption('edgeSnapDeadband',
                               section="user-interface",
                               key="edge-snap-deadband",
                               default=5,
                               notify=True)

PreferencesDialog.addNumericPreference('edgeSnapDeadband',
                                       section=_("Behavior"),
                                       label=_("Snap distance"),
                                       description=_("Threshold (in pixels) at which two clips will snap together "
                                                     "when dragging or trimming."),
                                       lower=0)

GlobalSettings.addConfigOption('imageClipLength',
                               section="user-interface",
                               key="image-clip-length",
                               default=1000,
                               notify=True)

PreferencesDialog.addNumericPreference('imageClipLength',
                                       section=_("Behavior"),
                                       label=_("Image clip duration"),
                                       description=_(
                                           "Default clip length (in miliseconds) of images when inserting on the timeline."),
                                       lower=1)

"""
Convention throughout this file:
Every GES element which name could be mistaken with a UI element
is prefixed with a little b, example : bTimeline
"""


class VerticalBar(Gtk.DrawingArea, Loggable):
    """
    A simple vertical bar to be drawn on top of the timeline
    """
    __gtype_name__ = "PitiviVerticalBar"

    def __init__(self, css_class):
        super(VerticalBar, self).__init__()
        Loggable.__init__(self)
        self.get_style_context().add_class(css_class)

    def do_get_preferred_width(self):
        self.debug("Getting prefered height")
        return ui.PLAYHEAD_WIDTH, ui.PLAYHEAD_WIDTH

    def do_get_preferred_height(self):
        self.debug("Getting prefered height")
        return self.get_parent().get_allocated_height(), self.get_parent().get_allocated_height()


class Marquee(Gtk.Box, Loggable):
    """
    Marquee widget representing a selection area inside the timeline
    it should be drawn on top of the timeline layout.

    It provides an API that makes it easy to update its value directly
    from Gdk.Event
    """

    __gtype_name__ = "PitiviMarquee"

    def __init__(self, timeline):
        """
        @timeline: The #Timeline on which the marquee will
                   be used
        """
        super(Marquee, self).__init__()
        Loggable.__init__(self)

        self._timeline = timeline
        self.start_x = None
        self.start_y = None
        self.set_visible(False)

        self.get_style_context().add_class("Marquee")

    def hide(self):
        self.start_x = None
        self.start_y = None
        self.props.height_request = -1
        self.props.width_request = -1
        self.set_visible(False)

    def setStartPosition(self, event):
        event_widget = self._timeline.get_event_widget(event)
        x, y = event_widget.translate_coordinates(self._timeline, event.x, event.y)

        self.start_x, self.start_y = self._timeline.adjustCoords(x=x, y=y)

    def move(self, event):
        event_widget = self._timeline.get_event_widget(event)

        x, y = self._timeline.adjustCoords(coords=event_widget.translate_coordinates(self._timeline, event.x, event.y))

        start_x = min(x, self.start_x)
        start_y = min(y, self.start_y)

        self.get_parent().move(self, start_x, start_y)
        self.props.width_request = abs(self.start_x - x)
        self.props.height_request = abs(self.start_y - y)
        self.set_visible(True)

    def findSelected(self):
        x, y = self._timeline.layout.child_get(self, "x", "y")
        res = []

        w = self.props.width_request
        for layer in self._timeline.bTimeline.get_layers():
            intersects, unused_rect = Gdk.rectangle_intersect(layer.ui.get_allocation(), self.get_allocation())

            if not intersects:
                continue

            for clip in layer.get_clips():
                if self.contains(clip, x, w):
                    toplevel = clip.get_toplevel_parent()
                    if isinstance(toplevel, GES.Group) and toplevel != self._timeline.current_group:
                        res.extend([c for c in clip.get_toplevel_parent().get_children(True)
                                    if isinstance(c, GES.Clip)])
                    else:
                        res.append(clip)

        self.debug("Selected clips: %s" % res)

        return tuple(set(res))

    def contains(self, clip, marquee_start, marquee_width):
        if clip.ui is None:
            return False

        child_start = clip.ui.get_parent().child_get(clip.ui, "x")[0]
        child_end = child_start + clip.ui.get_allocation().width

        marquee_end = marquee_start + marquee_width

        if child_start <= marquee_start <= child_end:
            return True

        if child_start <= marquee_end <= child_end:
            return True

        if marquee_start <= child_start and marquee_end >= child_end:
            return True

        return False


class Timeline(Gtk.EventBox, timelineUtils.Zoomable, Loggable):
    """
    The main timeline Widget, it contains the representation of the GESTimeline
    without any extra widgets.
    """

    __gtype_name__ = "PitiviTimeline"

    def __init__(self, container, app):
        super(Timeline, self).__init__()

        timelineUtils.Zoomable.__init__(self)
        Loggable.__init__(self)

        self._main_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(self._main_hbox)

        self.layout = Gtk.Layout()
        self.hadj = self.layout.get_hadjustment()
        self.vadj = self.layout.get_vadjustment()

        self.__layers_controls_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Stuff the layers controls in a viewport so it can be scrolled.
        viewport = Gtk.Viewport(vadjustment=self.vadj)
        viewport.add(self.__layers_controls_vbox)

        # Make sure the viewport has no border or other decorations.
        viewport_style = viewport.get_style_context()
        for css_class in viewport_style.list_classes():
            viewport_style.remove_class(css_class)
        self._main_hbox.pack_start(viewport, False, False, 0)

        self._main_hbox.pack_start(self.layout, False, True, 0)
        self.get_style_context().add_class("Timeline")

        self.__layers_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.__layers_vbox.props.width_request = self.get_allocated_width()
        self.__layers_vbox.props.height_request = self.get_allocated_height()
        self.layout.put(self.__layers_vbox, 0, 0)

        self.bTimeline = None
        self.__last_position = 0
        self.selection = timelineUtils.Selection()

        self._layers = []
        self.parent = container
        self.app = app
        self.__snap_position = 0
        self._project = None

        self.current_group = None
        self.createSelectionGroup()

        self.__playhead = VerticalBar("PlayHead")
        self.__playhead.show()
        self.layout.put(self.__playhead, self.nsToPixel(self.__last_position), 0)

        self.__snap_bar = VerticalBar("SnapBar")
        self.layout.put(self.__snap_bar, 0, 0)

        self.__allow_seek = True

        self.__setupTimelineEdition()
        self.__setUpDragAndDrop()
        self.__setupSelectionMarquee()

        self.__button_pressed = False

        # Setup our Gtk.Widget properties
        self.add_events(Gdk.EventType.BUTTON_PRESS | Gdk.EventType.BUTTON_RELEASE)
        self.connect("scroll-event", self.__scrollEventCb)
        self.connect("button-press-event", self.__buttonPressEventCb)
        self.connect("button-release-event", self.__buttonReleaseEventCb)
        self.connect("motion-notify-event", self.__motionNotifyEventCb)
        self.connect("drag-motion", self.__dragMotionCb)
        self.connect("drag-leave", self.__dragLeaveCb)
        self.connect("drag-drop", self.__dragDropCb)
        self.connect("drag-data-received", self.__dragDataReceivedCb)

        self.props.expand = True
        self.get_accessible().set_name("timeline canvas")
        self.__fake_event_widget = None

    def sendFakeEvent(self, event, event_widget):
        # Member usefull for testsing
        self.__fake_event_widget = event_widget

        self.info("Faking %s" % event)
        if event.type == Gdk.EventType.BUTTON_PRESS:
            self.__buttonPressEventCb(self, event)
        elif event.type == Gdk.EventType.BUTTON_RELEASE:
            self.__buttonReleaseEventCb(self, event)
        elif event.type == Gdk.EventType.MOTION_NOTIFY:
            self.__motionNotifyEventCb(self, event)

        self.__fake_event_widget = None

    def get_event_widget(self, event):
        if self.__fake_event_widget:
            return self.__fake_event_widget

        return Gtk.get_event_widget(event)

    def __get_event_widget(self, event):
        if self.__fake_event_widget:
            return self.__fake_event_widget

        return Gtk.get_event_widget(event)

    @property
    def allowSeek(self):
        return self.__allow_seek

    @allowSeek.setter
    def allowSeek(self, value):
        self.debug("Setting AllowSeek to %s" % value)
        self.__allow_seek = value

    def createSelectionGroup(self):
        if self.current_group:
            GES.Container.ungroup(self.current_group, False)

        self.current_group = GES.Group()
        self.current_group.props.serialize = False

    def setProject(self, project):
        """
        Connects with the GES.Timeline holding the project.
        """
        self._project = project
        if self._project:
            self._project.pipeline.connect('position', self._positionCb)
            bTimeline = self._project.timeline
        else:
            bTimeline = None

        if self.bTimeline is not None:
            self.bTimeline.disconnect_by_func(self._durationChangedCb)
            self.bTimeline.disconnect_by_func(self._layerAddedCb)
            self.bTimeline.disconnect_by_func(self._layerRemovedCb)
            self.bTimeline.disconnect_by_func(self._snapCb)
            self.bTimeline.disconnect_by_func(self._snapEndedCb)
            for layer in self.bTimeline.get_layers():
                self._layerRemovedCb(self.bTimeline, layer)

            self.bTimeline.ui = None

        self.bTimeline = bTimeline

        if bTimeline is None:
            return

        for layer in bTimeline.get_layers():
            self._addLayer(layer)

        self.bTimeline.connect("notify::duration", self._durationChangedCb)
        self.bTimeline.connect("layer-added", self._layerAddedCb)
        self.bTimeline.connect("layer-removed", self._layerRemovedCb)
        self.bTimeline.connect("snapping-started", self._snapCb)
        self.bTimeline.connect("snapping-ended", self._snapEndedCb)
        self.bTimeline.ui = self

        self.queue_draw()

    def _durationChangedCb(self, bTimeline, pspec):
        self.queue_draw()

    def scrollToPlayhead(self,):
        if self.__button_pressed or self.parent.ruler.pressed:
            self.__button_pressed = False
            return

        self.hadj.set_value(self.nsToPixel(self.__last_position) -
                            (self.layout.get_allocation().width / 2))

    def _positionCb(self, unused_pipeline, position):
        if self.__last_position == position:
            return

        self.__last_position = position
        self.scrollToPlayhead()
        self.layout.move(self.__playhead, max(0, self.nsToPixel(self.__last_position)), 0)

    # snapping indicator
    def _snapCb(self, unused_timeline, unused_obj1, unused_obj2, position):
        """
        Display or hide a snapping indicator line
        """
        self.layout.move(self.__snap_bar, self.nsToPixel(position), 0)
        self.__snap_bar.show()
        self.__snap_position = position
        self.debug("-> Snap START!")

    def hideSnapBar(self):
        self.debug("-> Force hiding snap bar")
        self.__snap_position = 0
        self.__snap_bar.hide()

    def _snapEndedCb(self, *unused_args):
        self.hideSnapBar()

    # Gtk.Widget virtual methods implementation
    def do_get_preferred_height(self):
        natural_height = max(1, len(self._layers)) * (ui.LAYER_HEIGHT + 20)

        return ui.LAYER_HEIGHT, natural_height

    def do_draw(self, cr):
        if self.bTimeline:
            width = self._computeTheoricalWidth()
            if self.draggingElement:
                width = max(width, self.layout.props.width)

            self.layout.set_size(width, len(self.bTimeline.get_layers()) * 200)

        Gtk.EventBox.do_draw(self, cr)

        self.__drawSnapIndicator(cr)
        self.__drawPlayHead(cr)

        self.layout.propagate_draw(self.__marquee, cr)

    def __drawSnapIndicator(self, cr):
        if self.__snap_position > 0:
            self.__snap_bar.props.height_request = self.layout.props.height
            self.__snap_bar.props.width_request = ui.SNAPBAR_WIDTH

            self.layout.propagate_draw(self.__snap_bar, cr)
        else:
            self.__snap_bar.hide()

    def __drawPlayHead(self, cr):
        self.__playhead.props.height_request = self.layout.props.height
        self.__playhead.props.width_request = ui.PLAYHEAD_WIDTH

        self.layout.propagate_draw(self.__playhead, cr)

    # ------------- #
    # util methods  #
    # ------------- #
    def _computeTheoricalWidth(self):
        if self.bTimeline is None:
            return 100

        return self.nsToPixel(self.bTimeline.props.duration)

    def _getParentOfType(self, widget, _type):
        """
        Get a clip from a child widget, if the widget is a child of the clip
        """
        if isinstance(widget, _type):
            return widget

        parent = widget.get_parent()
        while parent is not None and parent != self:
            parent = parent.get_parent()

            if isinstance(parent, _type):
                return parent
        return None

    def adjustCoords(self, coords=None, x=None, y=None):
        """
        Adjust coordinates passed as parametter that are raw
        coordinates from the whole timeline into sensible
        coordinates inside the visible area of the timeline.
        """
        if coords:
            x = coords[0]
            y = coords[1]

        if x is not None:
            x += self.hadj.props.value
            x -= ui.CONTROL_WIDTH

        if y is not None:
            y += self.vadj.props.value

            if x is None:
                return y
        else:
            return x

        return x, y

    # Gtk events management
    def __scrollEventCb(self, unused_widget, event):
        res, delta_x, delta_y = event.get_scroll_deltas()
        if not res:
            return False

        event_widget = self.get_event_widget(event)
        x, y = event_widget.translate_coordinates(self, event.x, event.y)
        if event.get_state() & Gdk.ModifierType.SHIFT_MASK:
            if delta_y > 0:
                self.parent.scroll_down()
            elif delta_y < 0:
                self.parent.scroll_up()
        elif event.get_state() & Gdk.ModifierType.CONTROL_MASK:
            if delta_y > 0:
                timelineUtils.Zoomable.zoomOut()
                self.queue_draw()
            elif delta_y < 0:
                timelineUtils.Zoomable.zoomIn()
                self.queue_draw()
        return False

    def __buttonPressEventCb(self, unused_widget, event):
        event_widget = self.get_event_widget(event)

        self.debug("PRESSED %s" % event)
        self.__button_pressed = True

        res, button = event.get_button()
        if res and button == 1:
            self.draggingElement = self._getParentOfType(event_widget, Clip)
            self.debug("Dragging element is %s" % self.draggingElement)
            if self.draggingElement is not None:
                self.__drag_start_x = event.x

            else:
                self.__marquee.setStartPosition(event)

        return False

    def __buttonReleaseEventCb(self, unused_widget, event):
        if self.draggingElement:
            self.dragEnd()
        else:
            self._selectUnderMarquee()

        if self.allowSeek:
            event_widget = self.get_event_widget(event)
            x, unused_y = event_widget.translate_coordinates(self, event.x, event.y)
            x -= CONTROL_WIDTH
            x += self.hadj.get_value()

            position = self.pixelToNs(x)
            self._project.seeker.seek(position)

        self.allowSeek = True
        self._snapEndedCb()

        return False

    def __motionNotifyEventCb(self, unused_widget, event):
        if self.draggingElement:
            state = event.get_state()

            if isinstance(state, tuple):
                state = state[1]

            if not state & Gdk.ModifierType.BUTTON1_MASK:
                self.dragEnd()
                return False

            self.__dragUpdate(self.get_event_widget(event), event.x, event.y)
            self.got_dragged = True
        elif self.__marquee.start_x:
            self.__marquee.move(event)

        return False

    def _selectUnderMarquee(self):
        if self.__marquee.props.width_request > 0:
            clips = self.__marquee.findSelected()

            if clips:
                self.createSelectionGroup()

                for clip in clips:
                    self.current_group.add(clip.get_toplevel_parent())

                self.selection.setSelection(clips, timelineUtils.SELECT)
            else:
                self.selection.setSelection([], timelineUtils.SELECT)
        else:
            only_transitions = not bool([selected for selected in self.selection.selected
                                         if not isinstance(selected, GES.TransitionClip)])
            if not only_transitions:
                self.selection.setSelection([], timelineUtils.SELECT)

        self.__marquee.hide()

    def updatePosition(self):
        for layer in self._layers:
            layer.updatePosition()

        self.queue_draw()

    def __setupSelectionMarquee(self):
        self.__marquee = Marquee(self)
        self.layout.put(self.__marquee, 0, 0)

    # drag and drop
    def __setUpDragAndDrop(self):
        self.got_dragged = False
        self.dropHighlight = False
        self.dropOccured = False
        self.dropDataReady = False
        self.dropData = None
        self._createdClips = False
        self.isDraggedClip = False
        self._lastClipOnLeave = None

        # To be able to receive effects dragged on clips.
        self.drag_dest_set(0, [ui.EFFECT_TARGET_ENTRY], Gdk.DragAction.COPY)
        # To be able to receive assets dragged from the media library.
        self.drag_dest_add_uri_targets()

    def createClip(self, x, y):
        if self.isDraggedClip and self._createdClips is False:

            # From the media library
            placement = 0
            for uri in self.dropData:
                asset = self.app.gui.medialibrary.getAssetForUri(uri)
                if asset is None:
                    break

                if asset.is_image():
                    clip_duration = self._settings.imageClipLength * \
                        Gst.SECOND / 1000.0
                else:
                    clip_duration = asset.get_duration()

                layer, on_sep = self.__getLayerAt(y)
                if not placement:
                    placement = self.pixelToNs(x)

                self.app.action_log.begin("add clip")
                bClip = layer.add_asset(asset,
                                        placement,
                                        0,
                                        clip_duration,
                                        asset.get_supported_formats())
                self.app.action_log.commit()

                self.draggingElement = bClip.ui
                self._createdClips = True

                return True

        return False

    def __dragMotionCb(self, unused_widget, context, x, y, timestamp):

        target = self.drag_dest_find_target(context, None)
        if not self.dropDataReady:
            # We don't know yet the details of what's being dragged.
            # Ask for the details.
            self.drag_get_data(context, target, timestamp)
            Gdk.drag_status(context, 0, timestamp)
        else:
            if not self.createClip(x, y):
                self.__dragUpdate(self, x, y)

            Gdk.drag_status(context, Gdk.DragAction.COPY, timestamp)
            if not self.dropHighlight:
                self.drag_highlight()
                self.dropHighlight = True
        return True

    def __dragLeaveCb(self, unused_widget, unused_context, unused_timestamp):
        if self.draggingElement:
            self._lastClipOnLeave = (self.draggingElement.bClip.get_layer(), self.draggingElement.bClip)
            self.draggingElement.bClip.get_layer().remove_clip(self.draggingElement.bClip)
            self._createdClips = False

    def __dragDropCb(self, unused_widget, context, x, y, timestamp):
        # Same as in insertEnd: this value changes during insertion, snapshot
        # it
        zoom_was_fitted = self.parent.zoomed_fitted

        target = self.drag_dest_find_target(context, None)
        if target.name() == "text/uri-list":
            self.debug("Got list of URIs")
            if self._lastClipOnLeave:
                self.dropData = None
                self.dropDataReady = False

                layer, clip = self._lastClipOnLeave
                layer.add_clip(clip)

                if zoom_was_fitted:
                    self.parent._setBestZoomRatio()

                self.dragEnd()
        elif target.name() == "pitivi/effect":
            self.fixme("TODO Implement effect support")

        return True

    def __dragDataReceivedCb(self, unused_widget,
                             drag_context, unused_x,
                             unused_y, selection_data, unused_info, timestamp):
        dragging_effect = selection_data.get_data_type().name() == "pitivi/effect"
        if not self.dropDataReady:
            self._lastClipOnLeave = None
            if dragging_effect:
                # Dragging an effect from the Effect Library.
                factory_name = str(selection_data.get_data(), "UTF-8")
                self.dropData = factory_name
                self.dropDataReady = True
            elif selection_data.get_length() > 0:
                # Dragging assets from the Media Library.
                # if not self.dropOccured:
                #    self.timeline.resetGhostClips()
                self.dropData = selection_data.get_uris()
                self.dropDataReady = True

        if self.dropOccured:
            # The data was requested by the drop handler.
            self.dropOccured = False
            drag_context.finish(True, False, timestamp)
        else:
            # The data was requested by the move handler.
            self.isDraggedClip = not dragging_effect
            self._createdClips = False
            self.debug("Data received")

    # Handle layers
    def _layerAddedCb(self, timeline, bLayer):
        self._addLayer(bLayer)

    def _addLayer(self, bLayer):
        layer_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bLayer.control_ui = LayerControls(bLayer, self.app)
        bLayer.ui = Layer(bLayer, self)

        bLayer.ui.before_sep = SpacedSeparator()
        layer_widget.pack_start(bLayer.ui.before_sep, False, False, 5)

        self._layers.append(bLayer.ui)
        layer_widget.pack_start(bLayer.ui, True, True, 0)

        bLayer.ui.after_sep = SpacedSeparator()
        layer_widget.pack_start(bLayer.ui.after_sep, False, False, 5)

        self.__layers_vbox.pack_start(layer_widget, True, True, 0)
        self.__layers_controls_vbox.pack_start(bLayer.control_ui, False, False, 0)

        bLayer.ui.connect("remove-me", self._removeLayerCb)

        self.show_all()

    def _removeLayerCb(self, layer):
        self.bTimeline.remove_layer(layer.bLayer)

    def _removeLayer(self, bLayer):
        self.info("Removing layer: %s" % bLayer.props.priority)
        self.__layers_vbox.remove(bLayer.ui.get_parent())
        self.__layers_controls_vbox.remove(bLayer.control_ui)

        self._layers.remove(bLayer.ui)
        bLayer.ui = None
        bLayer.control_ui = None

        self._layers.sort(key=lambda layer: layer.bLayer.props.priority)
        i = 0
        self.debug("Reseting layers priorities")
        for layer in self._layers:
            layer.bLayer.props.priority = i

            self.__layers_vbox.child_set_property(layer.get_parent(),
                                                  "position",
                                                  layer.bLayer.props.priority)

            self.__layers_controls_vbox.child_set_property(layer.bLayer.control_ui,
                                                           "position",
                                                           layer.bLayer.props.priority)

            i += 1

    def _layerRemovedCb(self, unused_timeline, layer):
        self._removeLayer(layer)

    # Interface Zoomable
    def zoomChanged(self):
        self.debug("Zoom changed")
        self.updatePosition()
        self.layout.move(self.__playhead, self.nsToPixel(self.__last_position), 0)
        self.queue_draw()

    # Edition handling
    def __setupTimelineEdition(self):
        self.draggingElement = None
        self.__editing_context = None
        self.__got_dragged = False
        self.__drag_start_x = 0
        self.__on_separators = []
        self._on_layer = None

    def __getLayerAt(self, y, bLayer=None):
        if y < 20 or not self.bTimeline.get_layers():
            try:
                bLayer = self.bTimeline.get_layers()[0]
            except IndexError:
                bLayer = self.bTimeline.append_layer()

            self.debug("Returning very first layer")
            return bLayer, [bLayer.ui.before_sep]

        layers = self.bTimeline.get_layers()
        rect = Gdk.Rectangle()
        rect.x = 0
        rect.y = y
        rect.height = 1
        rect.width = 1
        for i in range(len(layers)):
            layer = layers[i]
            layer_alloc = layer.ui.get_allocation()

            if Gdk.rectangle_intersect(rect, layer_alloc)[0] is True:
                return layer, []

            separators = [layer.ui.after_sep]
            sep_rectangle = Gdk.Rectangle()
            sep_rectangle.x = 0
            sep_rectangle.y = layer_alloc.y + layer_alloc.height
            try:
                sep_rectangle.height = layers[i + 1].ui.get_allocation().y - \
                    layer_alloc.y - layer_alloc.height
                separators.append(layers[i + 1].ui.before_sep)
            except IndexError:
                sep_rectangle.height += ui.LAYER_HEIGHT

            if sep_rectangle.y <= rect.y <= sep_rectangle.y + sep_rectangle.height:
                self.debug("Returning layer %s, separators: %s" % (layer, separators))
                return layer, separators

        self.debug("Returning very last layer")

        return layers[-1], [layers[-1].ui.after_sep]

    def __setHoverSeparators(self):
        for sep in self.__on_separators:
            ui.set_children_state_recurse(sep, Gtk.StateFlags.PRELIGHT)

    def __unsetHoverSeparators(self):
        for sep in self.__on_separators:
            ui.unset_children_state_recurse(sep, Gtk.StateFlags.PRELIGHT)

    def __dragUpdate(self, event_widget, x, y):
        if self.__got_dragged is False:
            self.__got_dragged = True
            self.allowSeek = False
            self.__editing_context = timelineUtils.EditingContext(self.draggingElement.bClip,
                                                                  self.bTimeline,
                                                                  self.draggingElement.edit_mode,
                                                                  self.draggingElement.dragging_edge,
                                                                  None,
                                                                  self.app.action_log)

        x, y = event_widget.translate_coordinates(self, x, y)
        x -= ui.CONTROL_WIDTH
        x += self.hadj.get_value()
        y += self.vadj.get_value()

        mode = self.get_parent().getEditionMode(isAHandle=self.__editing_context.edge != GES.Edge.EDGE_NONE)
        self.__editing_context.setMode(mode)

        if self.__editing_context.edge is GES.Edge.EDGE_END:
            position = self.pixelToNs(x)
        else:
            position = self.pixelToNs(x - self.__drag_start_x)

        self.__unsetHoverSeparators()

        self._on_layer, self.__on_separators = self.__getLayerAt(y,
                                                                 self.draggingElement.bClip.get_layer())

        priority = self._on_layer.props.priority
        if self.__on_separators:
            self.__setHoverSeparators()

        self.__editing_context.editTo(position, priority)

    def createLayer(self, priority):
        self.info("Creating layer %s" % priority)
        new_bLayer = GES.Layer.new()
        new_bLayer.props.priority = priority
        self.bTimeline.add_layer(new_bLayer)

        bLayers = self.bTimeline.get_layers()
        if priority < len(bLayers):
            for bLayer in bLayers:
                if bLayer == new_bLayer:
                    continue

                if bLayer.get_priority() >= priority:
                    bLayer.props.priority += 1
                    self.__layers_vbox.child_set_property(bLayer.ui.get_parent(),
                                                          "position",
                                                          bLayer.props.priority)

                    self.__layers_controls_vbox.child_set_property(bLayer.control_ui,
                                                                   "position",
                                                                   bLayer.props.priority)

        self.__layers_vbox.child_set_property(new_bLayer.ui.get_parent(),
                                              "position",
                                              new_bLayer.props.priority)

        self.__layers_controls_vbox.child_set_property(new_bLayer.control_ui,
                                                       "position",
                                                       new_bLayer.props.priority)

        return new_bLayer

    def dragEnd(self):
        if self.draggingElement is not None and self.__got_dragged:
            self.debug("DONE dargging %s" % self.draggingElement)
            self._snapEndedCb()

            if self.__on_separators:
                priority = self._on_layer.props.priority
                if self.__on_separators[0] == self._on_layer.ui.after_sep:
                    priority = self._on_layer.props.priority + 1

                self.createLayer(max(0, priority))
                self._onSeparatorStartTime = None
                self.__editing_context.editTo(self.__editing_context.new_position, priority)
            self.layout.props.width = self._computeTheoricalWidth()

            self.__editing_context.finish()

        self.draggingElement = None
        self.__got_dragged = False
        self.__editing_context = None
        self.hideSnapBar()

        self.__unsetHoverSeparators()
        self.__on_separators = []

        self.queue_draw()


class TimelineContainer(Gtk.Grid, Zoomable, Loggable):

    """
    Container for zoom box, ruler, timeline, scrollbars and toolbar.
    """

    def __init__(self, gui, instance, ui_manager):
        Zoomable.__init__(self)
        Gtk.Grid.__init__(self)
        Loggable.__init__(self)

        # Allows stealing focus from other GTK widgets, prevent accidents:
        self.props.can_focus = True

        self.gui = gui
        self.ui_manager = ui_manager
        self.app = instance
        self._settings = self.app.settings

        self._projectmanager = None
        self._project = None
        self.bTimeline = None

        self.ui_manager.add_ui_from_file(
            os.path.join(get_ui_dir(), "timelinecontainer.xml"))
        self._createActions()
        self._createUi()

        self._settings.connect("edgeSnapDeadbandChanged",
                               self._snapDistanceChangedCb)

        self.show_all()

    # Public API

    def insertEnd(self, assets):
        """
        Allows to add any asset at the end of the current timeline.
        """
        self.app.action_log.begin("add clip")
        if self.bTimeline is None:
            raise TimelineError("No bTimeline set, this is a bug")

        layer = self._getLongestLayer()

        # We need to snapshot this value, because we only do the zoom fit at the
        # end of clip insertion, but inserting multiple clips eventually changes
        # the value of self.zoomed_fitted as clips get progressively
        # inserted...
        zoom_was_fitted = self.zoomed_fitted

        for asset in assets:
            if isinstance(asset, GES.TitleClip):
                clip_duration = asset.get_duration()
            elif asset.is_image():
                clip_duration = self._settings.imageClipLength * \
                    Gst.SECOND / 1000.0
            else:
                clip_duration = asset.get_duration()

            if not isinstance(asset, GES.TitleClip):
                layer.add_asset(asset, self.bTimeline.props.duration,
                                0, clip_duration, asset.get_supported_formats())
            else:
                asset.set_start(self.bTimeline.props.duration)
                layer.add_clip(asset)

        if zoom_was_fitted:
            self._setBestZoomRatio()
        else:
            self.scrollToPixel(
                Zoomable.nsToPixel(self.bTimeline.props.duration))

        self.app.action_log.commit()
        self._project.pipeline.commit_timeline()

    def purgeObject(self, asset_id):
        """Remove all instances of an asset from the timeline."""
        layers = self.bTimeline.get_layers()
        for layer in layers:
            for tlobj in layer.get_clips():
                if asset_id == tlobj.get_id():
                    layer.remove_clip(tlobj)
        self._project.pipeline.commit_timeline()

    def setProjectManager(self, projectmanager):
        if self._projectmanager is not None:
            self._projectmanager.disconnect_by_func(self._projectChangedCb)

        self._projectmanager = projectmanager

        if projectmanager is not None:
            projectmanager.connect(
                "new-project-created", self._projectCreatedCb)
            projectmanager.connect(
                "new-project-loaded", self._projectChangedCb)

    def zoomFit(self):
        # self._hscrollbar.set_value(0)
        self.app.write_action("set-zoom-fit", {"not-mandatory-action-type": True})

        self._setBestZoomRatio(allow_zoom_in=True)

    def scrollToPixel(self, x):
        if x > self.hadj.props.upper:
            # We can't scroll yet, because the canvas needs to be updated
            GLib.idle_add(self._scrollToPixel, x)
        else:
            self._scrollToPixel(x)

    def setProject(self, project):
        self._project = project
        if self._project:
            self._project.connect("rendering-settings-changed",
                                  self._renderingSettingsChangedCb)
            self.bTimeline = project.timeline
        else:
            self.bTimeline = None

        self.timeline.setProject(self._project)
        self.timeline.selection.connect(
            "selection-changed", self._selectionChangedCb)

    def getEditionMode(self, isAHandle=False):
        if self._shiftMask or self._autoripple_active:
            return GES.EditMode.EDIT_RIPPLE
        if isAHandle and self._controlMask:
            return GES.EditMode.EDIT_ROLL
        elif isAHandle:
            return GES.EditMode.EDIT_TRIM
        return GES.EditMode.EDIT_NORMAL

    def setActionsSensitivity(self, sensitive):
        """
        The timeline's "actions" have global keyboard shortcuts that are
        dangerous in any context other than the timeline. In a text entry widget
        for example, you don't want the "Delete" key to remove clips currently
        selected on the timeline, or "Spacebar" to toggle playback.

        This sets the sensitivity of all actiongroups that might interfere.
        """
        self.playhead_actions.set_sensitive(sensitive)
        self.debug("Playback shortcuts sensitivity set to %s", sensitive)

        sensitive = sensitive and self.timeline.selection
        self.selection_actions.set_sensitive(sensitive)
        self.debug("Editing shortcuts sensitivity set to %s", sensitive)

    # Internal API

    def _createUi(self):
        self.zoomBox = ZoomBox(self)
        self._shiftMask = False
        self._controlMask = False

        # self.stage.set_background_color(TIMELINE_BACKGROUND_COLOR)
        # self.timeline.set_position(CONTROL_WIDTH, 0)
        # self.controls.set_position(0, 0)

        # self.stage.add_child(self.controls)
        # self.stage.add_child(self.timeline)

        self.scrolled = 0

        self.zoomed_fitted = True

        self.timeline = Timeline(self, self.app)
        self.hadj = self.timeline.layout.get_hadjustment()
        self.vadj = self.timeline.layout.get_vadjustment()

        self._vscrollbar = Gtk.VScrollbar(adjustment=self.vadj)
        self._hscrollbar = Gtk.HScrollbar(adjustment=self.hadj)

        self.ruler = ScaleRuler(self, self.hadj)
        self.ruler.props.hexpand = True
        self.ruler.setProjectFrameRate(24.)
        self.ruler.hide()

        toolbar = self.ui_manager.get_widget("/TimelineToolBar")
        toolbar.get_style_context().add_class(Gtk.STYLE_CLASS_INLINE_TOOLBAR)
        toolbar.set_orientation(Gtk.Orientation.VERTICAL)
        toolbar.set_style(Gtk.ToolbarStyle.ICONS)
        toolbar.get_accessible().set_name("timeline toolbar")

        alter_style_class(".%s" % Gtk.STYLE_CLASS_INLINE_TOOLBAR, toolbar,
                          "padding-left: %dpx; border-width: 0px; background: alpha (@base_color, 0.0);" % (SPACING / 2))
        alter_style_class(
            ".%s.trough" % Gtk.STYLE_CLASS_SCROLLBAR, self._vscrollbar,
            "border: alpha (@base_color, 0.0); background: alpha (@base_color, 0.0);")
        alter_style_class(
            ".%s.trough" % Gtk.STYLE_CLASS_SCROLLBAR, self._hscrollbar,
            "border: alpha (@base_color, 0.0); background: alpha (@base_color, 0.0);")

        # Toggle/pushbuttons like the "gapless mode" ones are special, it seems
        # you can't insert them as normal "actions", so we create them here:
        gapless_mode_button = Gtk.ToggleToolButton()
        gapless_mode_button.set_stock_id("pitivi-gapless")
        gapless_mode_button.set_tooltip_markup(_("Toggle gapless mode\n"
                                                 "When enabled, adjacent clips automatically move to fill gaps."))
        toolbar.add(gapless_mode_button)
        # Restore the state of the timeline's "gapless" mode:
        self._autoripple_active = self._settings.timelineAutoRipple
        gapless_mode_button.set_active(self._autoripple_active)
        gapless_mode_button.connect("toggled", self._gaplessmodeToggledCb)

        self.attach(self.zoomBox, 0, 0, 1, 1)
        self.attach(self.ruler, 1, 0, 1, 1)
        self.attach(self.timeline, 0, 1, 2, 1)
        self.attach(self._vscrollbar, 2, 1, 1, 1)
        self.attach(self._hscrollbar, 1, 2, 1, 1)
        self.attach(toolbar, 3, 1, 1, 1)

        min_height = (self.ruler.get_size_request()[1] +
                      (EXPANDED_SIZE + SPACING) * 2 +
                      # Some more.
                      EXPANDED_SIZE)
        self.set_size_request(-1, min_height)
        self.set_margin_top(SPACING)

    def enableKeyboardAndMouseEvents(self):
        self.info("Unblocking timeline mouse and keyboard signals")
        self.stage.disconnect_by_func(self._ignoreAllEventsCb)

    def disableKeyboardAndMouseEvents(self):
        """
        A safety measure to prevent interacting with the timeline
        """
        self.info("Blocking timeline mouse and keyboard signals")
        self.timeline.connect("event", self._ignoreAllEventsCb)

    def _ignoreAllEventsCb(self, *unused_args):
        return True

    def _getLayers(self):
        """
        Make sure we have at least one layer in our timeline.
        """
        layers = self.bTimeline.get_layers()
        if not layers:
            layer = GES.Layer()
            layer.props.auto_transition = True
            self.bTimeline.add_layer(layer)
            return [layer]
        return layers

    def _getLongestLayer(self):
        """
        Return the longest layer.
        """
        layers = self._getLayers()
        if len(layers) == 1:
            return layers[0]
        # Create a list of (layer_length, layer) tuples.
        layer_lengths = [(max([(clip.get_start() + clip.get_duration()) for clip in layer.get_clips()] or [0]), layer)
                         for layer in layers]
        # Easily get the longest.
        unused_longest_time, longest_layer = max(layer_lengths)
        return longest_layer

    def _createActions(self):
        """
        Sets up the GtkActions. This allows managing the sensitivity of widgets
        to the mouse and keyboard shortcuts.
        """
        # TODO: use GAction + GActionGroup (Gio.SimpleAction +
        # Gio.SimpleActionGroup)

        # Action list items can vary in size (1-6 items). The first one is the
        # name, and it is the only mandatory option. All the other options are
        # optional, and if omitted will default to None.
        #
        # name (required), stock ID, translatable label,
        # keyboard shortcut, translatable tooltip, callback function
        zoom_in_tooltip = _("Zoom In")
        zoom_out_tooltip = _("Zoom Out")
        zoom_fit_tooltip = _("Zoom Fit")
        actions = (
            ("ZoomIn", Gtk.STOCK_ZOOM_IN, None,
             "<Control>plus", zoom_in_tooltip, self._zoomInCb),

            ("ZoomOut", Gtk.STOCK_ZOOM_OUT, None,
             "<Control>minus", zoom_out_tooltip, self._zoomOutCb),

            ("ZoomFit", Gtk.STOCK_ZOOM_FIT, None,
             "<Control>0", zoom_fit_tooltip, self._zoomFitCb),

            # Alternate keyboard shortcuts to the actions above
            ("ControlEqualAccel", Gtk.STOCK_ZOOM_IN, None,
             "<Control>equal", zoom_in_tooltip, self._zoomInCb),

            ("ControlKPAddAccel", Gtk.STOCK_ZOOM_IN, None,
             "<Control>KP_Add", zoom_in_tooltip, self._zoomInCb),

            ("ControlKPSubtractAccel", Gtk.STOCK_ZOOM_OUT, None,
             "<Control>KP_Subtract", zoom_out_tooltip, self._zoomOutCb),
        )

        selection_actions = (
            ("DeleteObj", Gtk.STOCK_DELETE, None,
             "Delete", _("Delete Selected"), self._deleteSelected),

            ("UngroupObj", "pitivi-ungroup", _("Ungroup"),
             "<Shift><Control>G", _("Ungroup clips"), self._ungroupSelected),

            # Translators: This is an action, the title of a button
            ("GroupObj", "pitivi-group", _("Group"),
             "<Control>G", _("Group clips"), self._groupSelected),

            ("AlignObj", "pitivi-align", _("Align"),
             "<Shift><Control>A", _("Align clips based on their soundtracks"), self._alignSelected),
        )

        playhead_actions = (
            ("PlayPause", Gtk.STOCK_MEDIA_PLAY, None,
             "space", _("Start Playback"), self._playPauseCb),

            ("Split", "pitivi-split", _("Split"),
             "S", _("Split clip at playhead position"), self._splitCb),

            ("Keyframe", "pitivi-keyframe", _("Add a Keyframe"),
             "K", _("Add a keyframe"), self._keyframeCb),
        )

        actiongroup = Gtk.ActionGroup(name="timelinepermanent")
        self.selection_actions = Gtk.ActionGroup(name="timelineselection")
        self.playhead_actions = Gtk.ActionGroup(name="timelineplayhead")

        actiongroup.add_actions(actions)

        self.ui_manager.insert_action_group(actiongroup, 0)
        self.selection_actions.add_actions(selection_actions)
        self.selection_actions.set_sensitive(False)
        self.ui_manager.insert_action_group(self.selection_actions, -1)
        self.playhead_actions.add_actions(playhead_actions)
        self.ui_manager.insert_action_group(self.playhead_actions, -1)

    def _setBestZoomRatio(self, allow_zoom_in=False):
        """
        Set the zoom level so that the entire timeline is in view.
        """
        ruler_width = self.ruler.get_allocation().width
        duration = 0 if not self.bTimeline else self.bTimeline.get_duration()
        if not duration:
            return

        # Add Gst.SECOND - 1 to the timeline duration to make sure the
        # last second of the timeline will be in view.
        timeline_duration = duration + Gst.SECOND - 1
        timeline_duration_s = int(timeline_duration / Gst.SECOND)
        self.debug(
            "Adjusting zoom for a timeline duration of %s secs", timeline_duration_s)

        ideal_zoom_ratio = float(ruler_width) / timeline_duration_s
        nearest_zoom_level = Zoomable.computeZoomLevel(ideal_zoom_ratio)
        if nearest_zoom_level >= Zoomable.getCurrentZoomLevel():
            # This means if we continue we'll zoom in.
            if not allow_zoom_in:
                # For example when the user zoomed out and is adding clips
                # to the timeline, zooming in would be confusing.
                self.log(
                    "Zoom not changed because the entire timeline is already visible")

                return

        Zoomable.setZoomLevel(nearest_zoom_level)
        self.bTimeline.set_snapping_distance(
            Zoomable.pixelToNs(self._settings.edgeSnapDeadband))

        # Only do this at the very end, after updating the other widgets.
        self.log("Setting 'zoomed_fitted' to True")
        self.zoomed_fitted = True

    def scroll_left(self):
        # This method can be a callback for our events, or called by ruler.py
        self._hscrollbar.set_value(self._hscrollbar.get_value() -
                                   self.hadj.props.page_size ** (2.0 / 3.0))

    def scroll_right(self):
        # This method can be a callback for our events, or called by ruler.py
        self._hscrollbar.set_value(self._hscrollbar.get_value() +
                                   self.hadj.props.page_size ** (2.0 / 3.0))

    def scroll_up(self):
        self._vscrollbar.set_value(self._vscrollbar.get_value() -
                                   self.vadj.props.page_size ** (2.0 / 3.0))

    def scroll_down(self):
        self._vscrollbar.set_value(self._vscrollbar.get_value() +
                                   self.vadj.props.page_size ** (2.0 / 3.0))

    def _scrollToPixel(self, x):
        if x > self.hadj.props.upper:
            self.warning(
                "Position %s is bigger than the hscrollbar's upper bound (%s) - is the position really in pixels?" %
                (x, self.hadj.props.upper))
        elif x < self.hadj.props.lower:
            self.warning(
                "Position %s is smaller than the hscrollbar's lower bound (%s)" %
                (x, self.hadj.props.lower))

        if self._project and self._project.pipeline.getState() != Gst.State.PLAYING:
            self.error("FIXME What should be done here?")

        self._hscrollbar.set_value(x)
        if self._project and self._project.pipeline.getState() != Gst.State.PLAYING:
            self.error("FIXME What should be done here?")

        self.timeline.updatePosition()
        self.timeline.queue_draw()
        return False

    def scrollToPlayhead(self):
        self.timeline.scrollToPlayhead()

    def _deleteSelected(self, unused_action):
        if self.bTimeline:
            self.app.action_log.begin("delete clip")

            for clip in self.timeline.selection:
                layer = clip.get_layer()
                if isinstance(clip, GES.TransitionClip):
                    continue
                layer.remove_clip(clip)

            self._project.pipeline.commit_timeline()
            self.app.action_log.commit()

    def _ungroupSelected(self, unused_action):
        if self.bTimeline:
            self.app.action_log.begin("ungroup")

            containers = set({})

            for obj in self.timeline.selection:
                toplevel = obj.get_toplevel_parent()
                if toplevel == self.timeline.current_group:
                    for child in toplevel.get_children(False):
                        containers.add(child)
                    toplevel.ungroup(False)
                else:
                    containers.add(toplevel)

            for container in containers:
                was_clip = isinstance(container, GES.Clip)
                clips = GES.Container.ungroup(container, False)
                if not was_clip:
                    continue

                new_layers = {}
                for clip in clips:
                    if isinstance(clip, GES.Clip):
                        all_audio = True
                        for child in clip.get_children(True):
                            if child.get_track_type() != GES.TrackType.AUDIO:
                                all_audio = False
                                break

                        if not all_audio:
                            self.debug("Not all audio, not moving anything to a new layer")

                            continue

                        new_layer = new_layers.get(clip.get_layer().get_priority(), None)
                        if not new_layer:
                            new_layer = self.timeline.createLayer(clip.get_layer().get_priority() + 1)
                            new_layers[clip.get_layer().get_priority()] = new_layer
                        self.info("Moving audio audio clip %s to new layer %s" % (clip, new_layer))
                        clip.move_to_layer(new_layer)

                self._project.pipeline.commit_timeline()

            self.timeline.createSelectionGroup()

            self.app.action_log.commit()
            self._project.pipeline.commit_timeline()

    def _groupSelected(self, unused_action):
        if self.bTimeline:
            self.app.action_log.begin("group")

            containers = set({})

            for obj in self.timeline.selection:
                toplevel = obj.get_toplevel_parent()
                if toplevel == self.timeline.current_group:
                    for child in toplevel.get_children(False):
                        containers.add(child)
                    toplevel.ungroup(False)
                else:
                    containers.add(toplevel)

            if containers:
                GES.Container.group(list(containers))

            self.timeline.createSelectionGroup()

            self._project.pipeline.commit_timeline()
            self.app.action_log.commit()

    def _alignSelected(self, unused_action):
        if not self.bTimeline:
            self.error(
                "Trying to use the autoalign feature with an empty timeline")
            return

        progress_dialog = AlignmentProgressDialog(self.app)
        progress_dialog.window.show()
        self.app.action_log.begin("align")

        def alignedCb():  # Called when alignment is complete
            self.app.action_log.commit()
            self._project.pipeline.commit_timeline()
            progress_dialog.window.destroy()

        auto_aligner = AutoAligner(self.timeline.selection, alignedCb)
        try:
            progress_meter = auto_aligner.start()
            progress_meter.addWatcher(progress_dialog.updatePosition)
        except Exception as e:
            self.error("Could not start the autoaligner: %s" % e)
            progress_dialog.window.destroy()

    def _splitCb(self, unused_action):
        """
        If clips are selected, split them at the current playhead position.
        Otherwise, split all clips at the playhead position.
        """
        selected = self.timeline.selection.getSelectedTrackElements()

        if selected:
            self._splitElements(selected)
        else:
            for track in self.bTimeline.get_tracks():
                self._splitElements(track.get_elements())

        self.timeline.hideSnapBar()
        self._project.pipeline.commit_timeline()

    def _splitElements(self, elements):
        position = self._project.pipeline.getPosition()
        for element in elements:
            start = element.get_start()
            end = start + element.get_duration()
            if start < position and end > position:
                clip = element.get_parent()
                clip.get_layer().splitting_object = True

                self.app.write_action("split-clip", {
                    "clip-name": clip.get_name(),
                    "position": float(position / Gst.SECOND)})

                clip.split(position)
                clip.get_layer().splitting_object = False

    def _keyframeCb(self, unused_action):
        """
        Add or remove a keyframe at the current position of the selected clip.
        """
        selected = self.timeline.selection.getSelectedTrackElements()

        for obj in selected:
            keyframe_exists = False
            position = self._project.pipeline.getPosition()
            position_in_obj = (position - obj.props.start) + obj.props.in_point
            interpolators = obj.getInterpolators()
            for value in interpolators:
                interpolator = obj.getInterpolator(value)
                keyframes = interpolator.getInteriorKeyframes()
                for kf in keyframes:
                    if kf.getTime() == position_in_obj:
                        keyframe_exists = True
                        self.app.action_log.begin("remove volume point")
                        interpolator.removeKeyframe(kf)
                        self.app.action_log.commit()
                if keyframe_exists is False:
                    self.app.action_log.begin("add volume point")
                    interpolator.newKeyframe(position_in_obj)
                    self.app.action_log.commit()

    def _playPauseCb(self, unused_action):
        self._project.pipeline.togglePlayback()

    def transposeXY(self, x, y):
        height = self.ruler.get_allocation().height
        x += self.timeline.get_scroll_point().x
        return x - CONTROL_WIDTH, y - height

    # Zoomable

    def zoomChanged(self):
        if self.bTimeline:
            # zoomChanged might be called various times before the UI is ready
            self.bTimeline.set_snapping_distance(
                Zoomable.pixelToNs(self._settings.edgeSnapDeadband))

    # Gtk widget virtual methods

    def do_key_press_event(self, event):
        # This is used both for changing the selection modes and for affecting
        # the seek keyboard shortcuts further below
        if event.keyval == Gdk.KEY_Shift_L:
            self._shiftMask = True
        elif event.keyval == Gdk.KEY_Control_L:
            self._controlMask = True

        # Now the second (independent) part: framestepping and seeking
        # shortcuts
        if event.keyval == Gdk.KEY_Left:
            if self._shiftMask:
                self._seeker.seekRelative(0 - Gst.SECOND)
            else:
                self._project.pipeline.stepFrame(self._framerate, -1)
        elif event.keyval == Gdk.KEY_Right:
            if self._shiftMask:
                self._seeker.seekRelative(Gst.SECOND)
            else:
                self._project.pipeline.stepFrame(self._framerate, 1)

    def do_key_release_event(self, event):
        if event.keyval == Gdk.KEY_Shift_L:
            self._shiftMask = False
        elif event.keyval == Gdk.KEY_Control_L:
            self._controlMask = False

    def do_focus_in_event(self, unused_event):
        self.log("Timeline has grabbed focus")
        self.setActionsSensitivity(True)

    def do_focus_out_event(self, unused_event):
        self.log("Timeline has lost focus")
        self.setActionsSensitivity(False)

    def __buttonPressCb(self, unused_event):
        self.pressed = True
        self.grab_focus()  # Prevent other widgets from being confused

    # Callbacks
    def _renderingSettingsChangedCb(self, project, item, value):
        """
        Called when any Project metadata changes, we filter out the one
        we are interested in.

        if @item is None, it mean we called it ourself, and want to force
        getting the project videorate value
        """
        if item == "videorate" or item is None:
            if value is None:
                value = project.videorate
            self._framerate = value

            self.ruler.setProjectFrameRate(self._framerate)

        if item == "width" or item == "height" or item == "videorate":
            project.update_restriction_caps()

    def _snapDistanceChangedCb(self, unused_settings):
        if self.bTimeline:
            self.bTimeline.set_snapping_distance(
                Zoomable.pixelToNs(self._settings.edgeSnapDeadband))

    def _projectChangedCb(self, unused_app, project, unused_fully_loaded):
        """
        When a project is loaded, we connect to its pipeline
        """
        assert self._project is project
        if self._project:
            self._seeker = self._project.seeker
            self.ruler.setPipeline(self._project.pipeline)

            self.ruler.setProjectFrameRate(self._project.videorate)
            self.ruler.zoomChanged()

            self._renderingSettingsChangedCb(self._project, None, None)
            self._setBestZoomRatio()
            if self.bTimeline:
                self.bTimeline.set_snapping_distance(
                    Zoomable.pixelToNs(self._settings.edgeSnapDeadband))

    def _projectCreatedCb(self, unused_app, project):
        """
        When a project is created, we connect to it timeline
        """
        if self._project:
            self._project.disconnect_by_func(self._renderingSettingsChangedCb)
            try:
                self.timeline._pipeline.disconnect_by_func(
                    self.timeline.positionCb)
            except AttributeError:
                pass
            except TypeError:
                pass  # We were not connected no problem

            self.timeline._pipeline = None
            self._seeker = None

        self.setProject(project)

    def _zoomInCb(self, unused_action):
        Zoomable.zoomIn()
        self.log("Setting 'zoomed_fitted' to False")
        self.zoomed_fitted = False

    def _zoomOutCb(self, unused_action):
        Zoomable.zoomOut()
        self.log("Setting 'zoomed_fitted' to False")
        self.zoomed_fitted = False

    def _zoomFitCb(self, unused_action):
        self.zoomFit()

    def _selectionChangedCb(self, selection):
        """
        The selected clips on the timeline canvas have changed with the
        "selection-changed" signal.

        This is where you apply global UI changes, unlike individual
        track elements' "selected-changed" signal from the Selected class.
        """
        if selection:
            self.selection_actions.set_sensitive(True)
        else:
            self.selection_actions.set_sensitive(False)

    def _gaplessmodeToggledCb(self, button):
        if button.get_active():
            self.info("Automatic ripple activated")
            self._autoripple_active = True
        else:
            self.info("Automatic ripple deactivated")
            self._autoripple_active = False
        self._settings.timelineAutoRipple = self._autoripple_active
