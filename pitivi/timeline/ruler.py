# -*- coding: utf-8 -*-
# Pitivi video editor
#
#       pitivi/timeline/ruler.py
#
# Copyright (c) 2006, Edward Hervey <bilboed@bilboed.com>
# Copyright (c) 2014, Alex Băluț <alexandru.balut@gmail.com>
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

import cairo
import os

from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import Gst
from gi.repository import Gtk

from gettext import gettext as _

from pitivi import configure
from pitivi.utils.loggable import Loggable
from pitivi.utils.timeline import Zoomable
from pitivi.utils.ui import NORMAL_FONT, PLAYHEAD_WIDTH, set_cairo_color, time_to_string, beautify_length


# Tuples of:
# - an interval lengths in seconds for which a timestamp will be displayed
# - how the ticks should be displayed for this interval:
#   (count per interval, height ratio) tuples.
SCALES = (
    (0.1, ((0.1, 1.0), (0.05, .5), (0.01, .25))),
    (0.2, ((0.2, 1.0), (0.1, .5), (0.05, .25))),
    (0.5, ((0.5, 1.0), (0.1, .25))),

    # 1 second.
    (1, ((1, 1.0), (0.5, .5), (0.1, .25))),
    (2, ((2, 1.0), (1, .5), (0.5, .25))),
    (5, ((5, 1.0), (1, .25))),
    (10, ((10, 1.0), (5, .5), (1, .25))),
    (20, ((20, 1.0), (10, .5), (1, .25))),
    (30, ((30, 1.0), (10, .5), (1, .25))),

    # 1 minute.
    (60, ((60, 1.0), (30, .5), (15, .25))),
    # 2 minutes.
    (120, ((120, 1.0), (60, .5), (30, .25))),
    # 5 minutes.
    (300, ((300, 1.0), (60, .25))),
    # 10 minutes.
    (600, ((600, 1.0), (300, .5), (60, .25))),
    # 30 minutes.
    (1800, ((1800, 1.0), (900, .5), (450, .25))),

    # 1 hour.
    (3600, ((3600, 1.0), (1800, .75), (900, .5))),
)

# The minimum distance between adjacent ticks.
MIN_TICK_SPACING_PIXELS = 6

# For displaying the times a bit to the right.
TIMES_LEFT_MARGIN_PIXELS = 3

# The minimum width for a frame to be displayed.
FRAME_MIN_WIDTH_PIXELS = 5
# How short it should be.
FRAME_HEIGHT_PIXELS = 5

NORMAL_FONT_SIZE = 13
SMALL_FONT_SIZE = 11


class ScaleRuler(Gtk.DrawingArea, Zoomable, Loggable):

    """
    Widget for displaying the ruler.

    Displays a series of consecutive intervals. For each interval its beginning
    time is shown. If zoomed in enough, shows the frames in alternate colors.

    @type timeline: L{pitivi.timeline.timeline.TimelineContainer}
    @type _pipeline: L{pitivi.utils.pipeline.Pipeline}
    """

    def __init__(self, timeline, hadj):
        Gtk.DrawingArea.__init__(self)
        Zoomable.__init__(self)
        Loggable.__init__(self)
        self.log("Creating new ScaleRuler")

        self.timeline = timeline
        self._pipeline = None
        self.hadj = hadj
        hadj.connect("value-changed", self._hadjValueChangedCb)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK |
                        Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.SCROLL_MASK)

        self.pixbuf = None

        # all values are in pixels
        self.pixbuf_offset = 0
        self.pixbuf_offset_painted = 0

        self.position = 0  # In nanoseconds
        self.frame_rate = Gst.Fraction(1 / 1)
        self.ns_per_frame = float(1 / self.frame_rate) * Gst.SECOND

        self.marker_pixbuf = GdkPixbuf.Pixbuf.new_from_file(
            os.path.join(configure.get_pixmap_dir(), "pitivi-marker.svg"))

        self.scales = SCALES

    def _hadjValueChangedCb(self, unused_arg):
        self.pixbuf_offset = self.hadj.get_value()
        self.queue_draw()

# Zoomable interface override

    def zoomChanged(self):
        self.queue_draw()

# Timeline position changed method

    def setPipeline(self, pipeline):
        self._pipeline = pipeline
        self._pipeline.connect('position', self.timelinePositionCb)

    def timelinePositionCb(self, unused_pipeline, position):
        self.position = position
        self.queue_draw()

# Gtk.Widget overrides

    def do_configure_event(self, unused_event):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        self.debug("Configuring, height %d, width %d", width, height)

        # Destroy previous buffer
        if self.pixbuf is not None:
            self.pixbuf.finish()
            self.pixbuf = None

        # Create a new buffer
        self.pixbuf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)

        style = self.app.gui.get_style_context()
        color_normal = style.get_color(Gtk.StateFlags.NORMAL)
        color_insensitive = style.get_color(Gtk.StateFlags.INSENSITIVE)
        self._color_normal = color_normal
        self._color_dimmed = Gdk.RGBA(
            *[(x * 3 + y * 2) / 5
              for x, y in ((color_normal.red, color_insensitive.red),
                           (color_normal.green, color_insensitive.green),
                           (color_normal.blue, color_insensitive.blue))])

        return False

    def do_draw(self, context):
        if self.pixbuf is None:
            self.info('No buffer to paint')
            return False

        pixbuf = self.pixbuf

        # Draw on a temporary context and then copy everything.
        drawing_context = cairo.Context(pixbuf)
        self.drawBackground(drawing_context)
        self.drawRuler(drawing_context)
        self.drawPosition(drawing_context)
        pixbuf.flush()

        context.set_source_surface(self.pixbuf, 0.0, 0.0)
        context.paint()

        return False

    def do_button_press_event(self, event):
        if not self._pipeline:
            return False

        self.debug("button pressed at x:%d", event.x)
        button = event.button
        if button == 3 or (button == 1 and self.app.settings.leftClickAlsoSeeks):
            position = self.pixelToNs(event.x + self.pixbuf_offset)
            self._pipeline.simple_seek(position)
        return False

    def do_button_release_event(self, event):
        self.debug("button released at x:%d", event.x)
        self.app.gui.focusTimeline()
        return False

    def do_motion_notify_event(self, event):
        if not self._pipeline:
            return False

        position = self.pixelToNs(event.x + self.pixbuf_offset)

        seek_mask = Gdk.ModifierType.BUTTON3_MASK
        if self.app.settings.leftClickAlsoSeeks:
            seek_mask |= Gdk.ModifierType.BUTTON1_MASK
        if event.state & seek_mask:
            self.debug("motion at event.x %d", event.x)
            self._pipeline.simple_seek(position)

        human_time = beautify_length(position)
        cur_frame = int(position / self.ns_per_frame) + 1
        self.set_tooltip_text(human_time + "\n" + _("Frame #%d" % cur_frame))
        return False

    def do_scroll_event(self, event):
        self.timeline.timeline.do_scroll_event(event)

    def setProjectFrameRate(self, rate):
        """
        Set the lowest scale based on project framerate
        """
        self.frame_rate = rate
        self.ns_per_frame = float(Gst.SECOND / self.frame_rate)
        self.scales = (2 / rate, 5 / rate, 10 / rate) + SCALES

# Drawing methods

    def drawBackground(self, context):
        width = context.get_target().get_width()
        height = context.get_target().get_height()
        style_context = self.app.gui.get_style_context()
        Gtk.render_background(style_context, context, 0, 0, width, height)

    def drawRuler(self, context):
        context.set_font_face(NORMAL_FONT)
        context.set_font_size(NORMAL_FONT_SIZE)

        spacing, interval_seconds, ticks = self._getSpacing(context)
        offset = self.pixbuf_offset % spacing
        self.drawFrameBoundaries(context)
        self.drawTicks(context, offset, spacing, interval_seconds, ticks)
        self.drawMarkers(context)
        self.drawTimes(context, offset, spacing, interval_seconds)

    def _getSpacing(self, context):
        # The longest timestamp we display is 0:00:00 because
        # when we display millis, they are displayed by themselves.
        min_interval_width = context.text_extents("0:00:00")[2] * 1.3
        zoom = Zoomable.zoomratio
        for interval_seconds, ticks in SCALES:
            interval_width = interval_seconds * zoom
            if interval_width >= min_interval_width:
                return interval_width, interval_seconds, ticks
        raise Exception(
            "Failed to find an interval size for textwidth:%s, zoomratio:%s" %
            (min_interval_width, Zoomable.zoomratio))

    def drawTicks(self, context, offset, spacing, interval_seconds, ticks):
        for tick_interval, height_ratio in ticks:
            count_per_interval = interval_seconds / tick_interval
            space = spacing / count_per_interval
            if space < MIN_TICK_SPACING_PIXELS:
                break
            paintpos = 0.5 - offset
            set_cairo_color(context, self._color_normal)
            while paintpos < context.get_target().get_width():
                self._drawTick(context, paintpos, height_ratio)
                paintpos += space

    def _drawTick(self, context, paintpos, height_ratio):
        # We need to use 0.5 pixel offsets to get a sharp 1 px line in cairo
        paintpos = int(paintpos - 0.5) + 0.5
        target_height = context.get_target().get_height()
        y = int(target_height * (1 - height_ratio))
        context.set_line_width(1)
        context.move_to(paintpos, y)
        context.line_to(paintpos, target_height)
        context.close_path()
        context.stroke()

    def drawTimes(self, context, offset, spacing, interval_seconds):
        # figure out what the optimal offset is
        interval = int(Gst.SECOND * interval_seconds)
        current_time = self.pixelToNs(self.pixbuf_offset)
        paintpos = TIMES_LEFT_MARGIN_PIXELS
        if offset > 0:
            current_time = current_time - (current_time % interval) + interval
            paintpos += spacing - offset

        set_cairo_color(context, self._color_normal)
        y_bearing = context.text_extents("0")[1]

        def split(x):
            # Seven elements: h : mm : ss . mmm
            # Using negative indices because the first element (hour)
            # can have a variable length.
            return x[:-10], x[-10], x[-9:-7], x[-7], x[-6:-4], x[-4], x[-3:]

        previous = split(time_to_string(max(0, current_time - interval)))
        width = context.get_target().get_width()
        while paintpos < width:
            context.move_to(int(paintpos), 1 - y_bearing)
            current = split(time_to_string(int(current_time)))
            millis = current_time % Gst.SECOND > 0
            self._drawTime(context, current, previous, millis)
            previous = current
            paintpos += spacing
            current_time += interval

    def _drawTime(self, context, current, previous, millis):
        hour = int(current[0])
        for index, (element, previous_element) in enumerate(zip(current, previous)):
            if index <= 1 and not hour:
                # Don't draw hour if 0.
                continue
            if millis:
                # Draw only the millis.
                if index < 5:
                    continue
            else:
                # Don't draw the millis.
                if index == 5:
                    break
            if element == previous_element:
                color = self._color_dimmed
            else:
                color = self._color_normal
            set_cairo_color(context, color)
            # Display the millis with a smaller font.
            small = index >= 5
            if small:
                context.set_font_size(SMALL_FONT_SIZE)
            context.show_text(element)
            if small:
                context.set_font_size(NORMAL_FONT_SIZE)

    def drawFrameBoundaries(self, context):
        """
        Draw the alternating rectangles that represent the project frames at
        high zoom levels. These are based on the framerate set in the project
        settings, not the actual frames on a video codec level.
        """
        frame_width = self.nsToPixel(self.ns_per_frame)
        if not frame_width >= FRAME_MIN_WIDTH_PIXELS:
            return

        offset = self.pixbuf_offset % frame_width
        height = context.get_target().get_height()
        y = int(height - FRAME_HEIGHT_PIXELS)
        # INSENSITIVE is a dark shade of gray, but lacks contrast
        # SELECTED will be bright blue and more visible to represent frames
        style = self.get_style_context()
        states = [style.get_background_color(Gtk.StateFlags.ACTIVE),
                  style.get_background_color(Gtk.StateFlags.SELECTED)]

        frame_num = int(
            self.pixelToNs(self.pixbuf_offset) * float(self.frame_rate) / Gst.SECOND)
        paintpos = self.pixbuf_offset - offset
        max_pos = context.get_target().get_width() + self.pixbuf_offset
        while paintpos < max_pos:
            paintpos = self.nsToPixel(
                1 / float(self.frame_rate) * Gst.SECOND * frame_num)
            set_cairo_color(context, states[(frame_num + 1) % 2])
            context.rectangle(
                0.5 + paintpos - self.pixbuf_offset, y, frame_width, height)
            context.fill()
            frame_num += 1

    def drawPosition(self, context):
        self.drawMarker(context, self.position, (1.0, 0.0, 0.0, 0.4))

    def drawMarker(self, context, marker, rgba):
        height = self.pixbuf.get_height()
        # Add 0.5 so that the line center is at the middle of the pixel,
        # without this the line appears blurry.
        xpos = self.nsToPixel(marker) - self.pixbuf_offset + 0.5
        context.set_line_width(PLAYHEAD_WIDTH)
        context.set_source_rgb(rgba[0], rgba[1], rgba[2])
        context.move_to(xpos, height / 2)
        context.line_to(xpos, height)
        context.stroke()

        marker_width = self.marker_pixbuf.props.width
        marker_height = self.marker_pixbuf.props.height
        xpos -= marker_width / 2
        ypos = (height - marker_height) / 2
        Gdk.cairo_set_source_pixbuf(context, self.marker_pixbuf, xpos, ypos)
        context.paint()
        context.push_group()
        Gdk.cairo_set_source_pixbuf(context, self.marker_pixbuf, xpos, ypos)
        context.paint()
        src = context.pop_group()
        context.set_source_rgba(*rgba)
        context.mask(src)

    def drawMarkers(self, context):
        bTimeline = self.timeline.bTimeline
        if bTimeline is None:
            return

        start_ns = self.pixelToNs(self.pixbuf_offset)
        end_ns = start_ns + self.pixelToNs(context.get_target().get_width())
        markers = bTimeline.get_snapping_points(start_ns, end_ns)
        for marker in markers:
            self.drawMarker(context, marker, (0.0, 0.0, 1.0, 0.4))
