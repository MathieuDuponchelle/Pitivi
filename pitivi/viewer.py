# Pitivi video editor
#
#       ui/viewer.py
#
# Copyright (c) 2005, Edward Hervey <bilboed@bilboed.com>
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

import platform
from gi.repository import Gtk
from gi.repository import Clutter
from gi.repository import GtkClutter
from gi.repository import Gdk
from gi.repository import Gst
from gi.repository import GdkX11
from gi.repository import GObject
from gi.repository import GES
GdkX11  # pyflakes
from gi.repository import GstVideo
GstVideo    # pyflakes
import cairo

from gettext import gettext as _
from time import time
from math import pi

from pitivi.settings import GlobalSettings
from pitivi.utils.pipeline import Seeker, SimplePipeline
from pitivi.utils.ui import SPACING, hex_to_rgb
from pitivi.utils.widgets import TimeWidget
from pitivi.utils.loggable import Loggable
from pitivi.utils.misc import print_ns

GlobalSettings.addConfigSection("viewer")
GlobalSettings.addConfigOption("viewerDocked", section="viewer",
    key="docked",
    default=True)
GlobalSettings.addConfigOption("viewerWidth", section="viewer",
    key="width",
    default=320)
GlobalSettings.addConfigOption("viewerHeight", section="viewer",
    key="height",
    default=240)
GlobalSettings.addConfigOption("viewerX", section="viewer",
    key="x-pos",
    default=0)
GlobalSettings.addConfigOption("viewerY", section="viewer",
    key="y-pos",
    default=0)
GlobalSettings.addConfigOption("pointSize", section="viewer",
    key="point-size",
    default=25)
GlobalSettings.addConfigOption("clickedPointColor", section="viewer",
    key="clicked-point-color",
    default='ffa854')
GlobalSettings.addConfigOption("pointColor", section="viewer",
    key="point-color",
    default='49a0e0')


class PitiviViewer(Gtk.VBox, Loggable):
    """
    A Widget to control and visualize a Pipeline

    @ivar pipeline: The current pipeline
    @type pipeline: L{Pipeline}
    @ivar action: The action controlled by this Pipeline
    @type action: L{ViewAction}
    """
    __gtype_name__ = 'PitiviViewer'
    __gsignals__ = {
        "activate-playback-controls": (GObject.SignalFlags.RUN_LAST,
            None, (GObject.TYPE_BOOLEAN,)),
    }

    INHIBIT_REASON = _("Currently playing")

    def __init__(self, app, undock_action=None):
        Gtk.VBox.__init__(self)
        self.set_border_width(SPACING)
        self.app = app
        self.settings = app.settings
        self.system = app.system

        Loggable.__init__(self)
        self.log("New PitiviViewer")

        self.pipeline = None
        self._tmp_pipeline = None  # Used for displaying a preview when trimming

        self.sink = None
        self.docked = True

        # Only used for restoring the pipeline position after a live clip trim preview:
        self._oldTimelinePos = None

        self._haveUI = False

        self._createUi()
        self.target = self.internal
        self.undock_action = undock_action
        if undock_action:
            self.undock_action.connect("activate", self._toggleDocked)

            if not self.settings.viewerDocked:
                self.undock()

    def setPipeline(self, pipeline, position=None):
        """
        Set the Viewer to the given Pipeline.

        Properly switches the currently set action to that new Pipeline.

        @param pipeline: The Pipeline to switch to.
        @type pipeline: L{Pipeline}.
        @param position: Optional position to seek to initially.
        """
        self.debug("self.pipeline:%r", self.pipeline)

        self.seeker = Seeker()
        self._disconnectFromPipeline()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

        self.pipeline = pipeline
        if self.pipeline:
            self.pipeline.pause()
            self.seeker.seek(position)

            self.pipeline.connect("state-change", self._pipelineStateChangedCb)
            self.pipeline.connect("position", self._positionCb)
            self.pipeline.connect("duration-changed", self._durationChangedCb)

        self.sink = pipeline.video_overlay._clutter_sink
        self.target.sink = self.sink
        self._switch_output_window()
        self._setUiActive()

    def _disconnectFromPipeline(self):
        self.debug("pipeline:%r", self.pipeline)
        if self.pipeline is None:
            # silently return, there's nothing to disconnect from
            return

        self.pipeline.disconnect_by_func(self._pipelineStateChangedCb)
        self.pipeline.disconnect_by_func(self._positionCb)
        self.pipeline.disconnect_by_func(self._durationChangedCb)

        self.pipeline = None

    def _setUiActive(self, active=True):
        self.debug("active %r", active)
        self.set_sensitive(active)
        if self._haveUI:
            for item in [self.goToStart_button, self.back_button,
                         self.playpause_button, self.forward_button,
                         self.goToEnd_button, self.timecode_entry]:
                item.set_sensitive(active)
        if active:
            self.emit("activate-playback-controls", True)

    def _externalWindowDeleteCb(self, window, event):
        self.dock()
        return True

    def _externalWindowConfigureCb(self, window, event):
        self.settings.viewerWidth = event.width
        self.settings.viewerHeight = event.height
        self.settings.viewerX = event.x
        self.settings.viewerY = event.y

    def _videoRealized(self, widget):
        if widget == self.target:
            self._switch_output_window()

    def _createUi(self):
        """ Creates the Viewer GUI """
        # Drawing area
        # The aspect ratio gets overridden on startup by setDisplayAspectRatio
        self.aframe = Gtk.AspectFrame(xalign=0.5, yalign=1.0, ratio=4.0 / 3.0,
                                      obey_child=False)

        self.internal = ViewerWidget(self.app.settings)
        # Transformation boxed DISABLED
        # self.internal.init_transformation_events()
        self.aframe.add(self.internal)
        self.aframe.connect("size-allocate", self.internal.sizeCb)
        self.pack_start(self.aframe, True, True, 0)

        self.external_window = Gtk.Window()
        vbox = Gtk.VBox()
        vbox.set_spacing(SPACING)
        self.external_window.add(vbox)
        self.external = ViewerWidget(self.app.settings)
        vbox.pack_start(self.external, True, True, 0)
        self.external_window.connect("delete-event", self._externalWindowDeleteCb)
        self.external_window.connect("configure-event", self._externalWindowConfigureCb)
        self.external_vbox = vbox

        # Buttons/Controls
        bbox = Gtk.HBox()
        boxalign = Gtk.Alignment(xalign=0.5, yalign=0.5, xscale=0.0, yscale=0.0)
        boxalign.add(bbox)
        self.pack_start(boxalign, False, True, 0)

        self.goToStart_button = Gtk.ToolButton(Gtk.STOCK_MEDIA_PREVIOUS)
        self.goToStart_button.connect("clicked", self._goToStartCb)
        self.goToStart_button.set_tooltip_text(_("Go to the beginning of the timeline"))
        self.goToStart_button.set_sensitive(False)
        bbox.pack_start(self.goToStart_button, False, True, 0)

        self.back_button = Gtk.ToolButton(Gtk.STOCK_MEDIA_REWIND)
        self.back_button.connect("clicked", self._backCb)
        self.back_button.set_tooltip_text(_("Go back one second"))
        self.back_button.set_sensitive(False)
        bbox.pack_start(self.back_button, False, True, 0)

        self.playpause_button = PlayPauseButton()
        self.playpause_button.connect("play", self._playButtonCb)
        bbox.pack_start(self.playpause_button, False, True, 0)
        self.playpause_button.set_sensitive(False)

        self.forward_button = Gtk.ToolButton(Gtk.STOCK_MEDIA_FORWARD)
        self.forward_button.connect("clicked", self._forwardCb)
        self.forward_button.set_tooltip_text(_("Go forward one second"))
        self.forward_button.set_sensitive(False)
        bbox.pack_start(self.forward_button, False, True, 0)

        self.goToEnd_button = Gtk.ToolButton(Gtk.STOCK_MEDIA_NEXT)
        self.goToEnd_button.connect("clicked", self._goToEndCb)
        self.goToEnd_button.set_tooltip_text(_("Go to the end of the timeline"))
        self.goToEnd_button.set_sensitive(False)
        bbox.pack_start(self.goToEnd_button, False, True, 0)

        # current time
        self.timecode_entry = TimeWidget()
        self.timecode_entry.setWidgetValue(0)
        self.timecode_entry.set_tooltip_text(_('Enter a timecode or frame number\nand press "Enter" to go to that position'))
        self.timecode_entry.connectActivateEvent(self._entryActivateCb)
        bbox.pack_start(self.timecode_entry, False, 10, 0)
        self._haveUI = True

        # Identify widgets for AT-SPI, making our test suite easier to develop
        # These will show up in sniff, accerciser, etc.
        self.goToStart_button.get_accessible().set_name("goToStart_button")
        self.back_button.get_accessible().set_name("back_button")
        self.playpause_button.get_accessible().set_name("playpause_button")
        self.forward_button.get_accessible().set_name("forward_button")
        self.goToEnd_button.get_accessible().set_name("goToEnd_button")
        self.timecode_entry.get_accessible().set_name("timecode_entry")

        screen = Gdk.Screen.get_default()
        height = screen.get_height()
        if height >= 800:
            # show the controls and force the aspect frame to have at least the same
            # width (+110, which is a magic number to minimize dead padding).
            bbox.show_all()
            req = bbox.size_request()
            width = req.width
            height = req.height
            width += 110
            height = int(width / self.aframe.props.ratio)
            self.aframe.set_size_request(width, height)

        self.buttons = bbox
        self.buttons_container = boxalign
        # Prevent black frames and flickering while resizing or changing focus:
        self.internal.set_double_buffered(False)
        self.external.set_double_buffered(False)
        # We keep the ViewerWidget hidden initially, or the desktop wallpaper
        # would show through the non-double-buffered widget!
        self.internal.set_no_show_all(True)
        self.external.set_no_show_all(True)
        self.internal.connect("realize", self._videoRealized)
        self.external.connect("realize", self._videoRealized)
        self.show_all()
        self.external_vbox.show_all()

    def setDisplayAspectRatio(self, ratio):
        """
        Sets the DAR of the Viewer to the given ratio.

        @arg ratio: The aspect ratio to set on the viewer
        @type ratio: L{float}
        """
        self.debug("Setting ratio of %f [%r]", float(ratio), ratio)
        try:
            self.aframe.set_property("ratio", float(ratio))
        except:
            self.warning("could not set ratio !")

    def _entryActivateCb(self, entry):
        self._seekFromTimecodeWidget()

    def _seekFromTimecodeWidget(self):
        nanoseconds = self.timecode_entry.getWidgetValue()
        self.seeker.seek(nanoseconds)

    ## active Timeline calllbacks
    def _durationChangedCb(self, unused_pipeline, duration):
        if duration == 0:
            self._setUiActive(False)
        else:
            self._setUiActive(True)

    ## Control Gtk.Button callbacks

    def setZoom(self, zoom):
        """
        Zoom in or out of the transformation box canvas.
        This is called by clipproperties.
        """
        if self.target.box:
            maxSize = self.target.area
            width = int(float(maxSize.width) * zoom)
            height = int(float(maxSize.height) * zoom)
            area = ((maxSize.width - width) / 2,
                    (maxSize.height - height) / 2,
                    width, height)
            self.sink.set_render_rectangle(*area)
            self.target.box.update_size(area)
            self.target.zoom = zoom
            self.target.sink = self.sink
            self.target.renderbox()

    def _playButtonCb(self, unused_button, playing):
        self.app.current_project.pipeline.togglePlayback()

    def _goToStartCb(self, unused_button):
        self.seeker.seek(0)

    def _backCb(self, unused_button):
        # Seek backwards one second
        self.seeker.seekRelative(0 - Gst.SECOND)

    def _forwardCb(self, unused_button):
        # Seek forward one second
        self.seeker.seekRelative(Gst.SECOND)

    def _goToEndCb(self, unused_button):
        try:
            end = self.app.current_project.pipeline.getDuration()
        except:
            self.warning("Couldn't get timeline duration")
        try:
            self.seeker.seek(end)
        except:
            self.warning("Couldn't seek to the end of the timeline")

    ## public methods for controlling playback

    def undock(self):
        if not self.undock_action:
            self.error("Cannot undock because undock_action is missing.")
            return
        if not self.docked:
            return

        self.docked = False
        self.settings.viewerDocked = False
        self.undock_action.set_label(_("Dock Viewer"))
        self.target = self.external

        self.remove(self.buttons_container)
        self.external_vbox.pack_end(self.buttons_container, False, False, 0)
        self.external_window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.external_window.show()

        self.fullscreen_button = Gtk.ToggleToolButton(Gtk.STOCK_FULLSCREEN)
        self.fullscreen_button.set_tooltip_text(_("Show this window in fullscreen"))
        self.buttons.pack_end(self.fullscreen_button, expand=False, fill=False, padding=6)
        self.fullscreen_button.show()
        self.fullscreen_button.connect("toggled", self._toggleFullscreen)

        # if we are playing, switch output immediately
        if self.sink:
            self._switch_output_window()
        self.hide()
        self.external_window.move(self.settings.viewerX, self.settings.viewerY)
        self.external_window.resize(self.settings.viewerWidth, self.settings.viewerHeight)

    def dock(self):
        if not self.undock_action:
            self.error("Cannot dock because undock_action is missing.")
            return
        if self.docked:
            return
        self.docked = True
        self.settings.viewerDocked = True
        self.undock_action.set_label(_("Undock Viewer"))
        self.target = self.internal

        self.fullscreen_button.destroy()
        self.external_vbox.remove(self.buttons_container)
        self.pack_end(self.buttons_container, False, False, 0)
        self.show()
        # if we are playing, switch output immediately
        if self.sink:
            self._switch_output_window()
        self.external_window.hide()

    def _toggleDocked(self, action):
        if self.docked:
            self.undock()
        else:
            self.dock()

    def _toggleFullscreen(self, widget):
        if widget.get_active():
            self.external_window.hide()
            # GTK doesn't let us fullscreen utility windows
            self.external_window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            self.external_window.show()
            self.external_window.fullscreen()
            widget.set_tooltip_text(_("Exit fullscreen mode"))
        else:
            self.external_window.unfullscreen()
            widget.set_tooltip_text(_("Show this window in fullscreen"))
            self.external_window.hide()
            self.external_window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
            self.external_window.show()

    def _positionCb(self, unused_pipeline, position):
        """
        If the timeline position changed, update the viewer UI widgets.

        This is meant to be called either by the gobject timer when playing,
        or by mainwindow's _timelineSeekCb when the timer is disabled.
        """
        self.timecode_entry.setWidgetValue(position, False)

    def clipTrimPreview(self, tl_obj, position):
        """
        While a clip is being trimmed, show a live preview of it.
        """
        if isinstance(tl_obj, GES.TitleClip) or tl_obj.props.is_image or not hasattr(tl_obj, "get_uri"):
            self.log("%s is an image or has no URI, so not previewing trim" % tl_obj)
            return False

        clip_uri = tl_obj.props.uri
        cur_time = time()
        if not self._tmp_pipeline:
            self.debug("Creating temporary pipeline for clip %s, position %s",
                clip_uri, print_ns(position))

            self._oldTimelinePos = self.pipeline.getPosition()
            self._tmp_pipeline = Gst.ElementFactory.make("playbin", None)
            self._tmp_pipeline.set_property("uri", clip_uri)
            self.setPipeline(SimplePipeline(self._tmp_pipeline, self._tmp_pipeline))
            self._lastClipTrimTime = cur_time
        if (cur_time - self._lastClipTrimTime) > 0.2 and self.pipeline.getState() == Gst.State.PAUSED:
            # Do not seek more than once every 200 ms (for performance)
            self._tmp_pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, position)
            self._lastClipTrimTime = cur_time

    def clipTrimPreviewFinished(self):
        """
        After trimming a clip, reset the project pipeline into the viewer.
        """
        if self._tmp_pipeline is not None:
            self._tmp_pipeline.set_state(Gst.State.NULL)
            self._tmp_pipeline = None  # Free the memory
            self.setPipeline(self.app.current_project.pipeline, self._oldTimelinePos)
            self.debug("Back to old pipeline")

    def _pipelineStateChangedCb(self, pipeline, state):
        """
        When playback starts/stops, update the viewer widget,
        play/pause button and (un)inhibit the screensaver.

        This is meant to be called by mainwindow.
        """
        self.info("current state changed : %s", state)
        if int(state) == int(Gst.State.PLAYING):
            self.playpause_button.setPause()
            self.system.inhibitScreensaver(self.INHIBIT_REASON)
        elif int(state) == int(Gst.State.PAUSED):
            self.playpause_button.setPlay()
            self.system.uninhibitScreensaver(self.INHIBIT_REASON)
        else:
            self.sink = None
            self.system.uninhibitScreensaver(self.INHIBIT_REASON)

    def _switch_output_window(self):
        # Don't do anything if we don't have a pipeline
        if self.sink is None:
            return

        if self.target.get_realized():
            self.sink.props.texture = self.target.texture
        else:
            # Show the widget and wait for the realized callback
            self.target.show()


class TransformationBox(Clutter.Actor):
    def __init__(self):
        Clutter.Actor.__init__(self)
        self.set_background_color(Clutter.Color.new(255, 255, 255, 50))


class ViewerWidget(GtkClutter.Embed, Loggable):
    """
    Widget for displaying properly GStreamer video sink

    @ivar settings: The settings of the application.
    @type settings: L{GlobalSettings}
    """

    __gsignals__ = {}

    def __init__(self, settings=None):
        GtkClutter.Embed.__init__(self)
        self._stage = self.get_stage()
        self._trans_box = TransformationBox()
        self.texture = Clutter.Texture()
        self.texture.set_sync_size(False)
        self._stage.add_child(self.texture)
        self._stage.add_child(self._trans_box)
        Loggable.__init__(self)
        self.seeker = Seeker()
        self.settings = settings
        self.box = None
        self.stored = False
        self.area = None
        self.zoom = 1.0
        self.sink = None
        self.pixbuf = None
        self.pipeline = None
        self.transformation_properties = None
        # FIXME PyGi Styling with Gtk3
        #for state in range(Gtk.StateType.INSENSITIVE + 1):
            #self.modify_bg(state, self.style.black)

    def set_size(self, width, height):
        self._trans_box.set_size(width, height)
        self.texture.set_size(width, height)

    def sizeCb(self, widget, alloc):
        w = min(alloc.width, alloc.height * widget.props.ratio)
        h = min(alloc.height, alloc.width / widget.props.ratio)
        self.set_size(w, h)


class PlayPauseButton(Gtk.Button, Loggable):
    """
    Double state Gtk.Button which displays play/pause
    """
    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_LAST, None, (GObject.TYPE_BOOLEAN,))
    }

    def __init__(self):
        Gtk.Button.__init__(self)
        Loggable.__init__(self)
        self.image = Gtk.Image()
        self.add(self.image)
        self.playing = False
        self.setPlay()
        self.connect('clicked', self._clickedCb)

    def set_sensitive(self, value):
        Gtk.Button.set_sensitive(self, value)

    def _clickedCb(self, unused):
        self.playing = not self.playing
        self.emit("play", self.playing)

    def setPlay(self):
        """ display the play image """
        self.log("setPlay")
        self.playing = True
        self.set_image(Gtk.Image.new_from_stock(Gtk.STOCK_MEDIA_PLAY, Gtk.IconSize.BUTTON))
        self.set_tooltip_text(_("Play"))
        self.playing = False

    def setPause(self):
        self.log("setPause")
        """ display the pause image """
        self.playing = False
        self.set_image(Gtk.Image.new_from_stock(Gtk.STOCK_MEDIA_PAUSE, Gtk.IconSize.BUTTON))
        self.set_tooltip_text(_("Pause"))
        self.playing = True
