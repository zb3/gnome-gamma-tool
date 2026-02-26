#!/usr/bin/env python3
"""GTK3 GUI front-end for gnome-gamma-tool."""

import os
import sys
import time
import uuid

import gi

gi.require_version("Gtk", "3.0")

try:
    gi.require_version("Colord", "1.0")
    _colord_version_ok = True
except ValueError:
    _colord_version_ok = False

from gi.repository import GLib, Gio, GObject, Gtk, Pango

if _colord_version_ok:
    try:
        from gi.repository import Colord

        COLORD_AVAILABLE = True
    except ImportError:
        COLORD_AVAILABLE = False
else:
    COLORD_AVAILABLE = False

# ---------------------------------------------------------------------------
# Core constants (mirrored from gnome-gamma-tool.py)
# ---------------------------------------------------------------------------
N_SAMPLES = 256
OUR_PREFIX = "gnome-gamma-tool-"
TIMEOUT = 4
POLL_INTERVAL = 0.01


# ---------------------------------------------------------------------------
# Core logic (shared with gnome-gamma-tool.py)
# ---------------------------------------------------------------------------
def linear_map(x, smin, smax, dmin, dmax):
    return (x - smin) / (smax - smin) * (dmax - dmin) + dmin


def generate_vcgt(gamma, color_temperature, contrast, bmin, bmax):
    gamma_factor = [1 / float(ga) for ga in gamma]
    temp_color = Colord.ColorRGB()
    vcgt = [None] * N_SAMPLES

    Colord.color_get_blackbody_rgb_full(
        color_temperature, temp_color, Colord.ColorBlackbodyFlags.USE_PLANCKIAN
    )

    for i in range(N_SAMPLES):
        c = Colord.ColorRGB.new()
        c.R = temp_color.R * ((i / (N_SAMPLES - 1)) ** gamma_factor[0])
        c.G = temp_color.G * ((i / (N_SAMPLES - 1)) ** gamma_factor[1])
        c.B = temp_color.B * ((i / (N_SAMPLES - 1)) ** gamma_factor[2])

        c.R = max(0, min(contrast[0] * (c.R - 0.5) + 0.5, 1))
        c.G = max(0, min(contrast[1] * (c.G - 0.5) + 0.5, 1))
        c.B = max(0, min(contrast[2] * (c.B - 0.5) + 0.5, 1))

        c.R = linear_map(c.R, 0, 1, bmin[0], bmax[0])
        c.G = linear_map(c.G, 0, 1, bmin[1], bmax[1])
        c.B = linear_map(c.B, 0, 1, bmin[2], bmax[2])

        vcgt[i] = c

    return vcgt


def build_profile_data(profile_data, arg_signature, gamma, temperature, contrast, bmin, bmax):
    if not profile_data:
        profile_data = Colord.Icc.new()
        profile_data.create_default()

    title = "gamma-tool: %s" % arg_signature
    profile_data.set_description("", title)
    profile_data.set_model("", title)

    unique_id = str(uuid.uuid4())
    profile_data.add_metadata("uuid", unique_id)

    vcgt = generate_vcgt(gamma, temperature, contrast, bmin, bmax)
    profile_data.set_vcgt(vcgt)

    return profile_data, unique_id


class ProfileMgr:
    def __init__(self):
        self.cd = Colord.Client()
        self.cd.connect_sync()
        self.devices = self._get_display_devices()
        self.desired_profile_filename = None
        self.received_profile = None
        GObject.Object.connect(self.cd, "profile-added", self._on_profile_added)

    def _get_display_devices(self):
        display_devices = []
        for device in self.cd.get_devices_sync():
            device.connect_sync()
            if device.get_kind() == Colord.DeviceKind.DISPLAY:
                display_devices.append(device)
        return display_devices

    def get_device_count(self):
        return len(self.devices)

    def get_device_names(self):
        names = []
        for i, device in enumerate(self.devices):
            name = device.get_model() or device.get_vendor() or f"Display {i}"
            names.append(name)
        return names

    def connect_device(self, device_idx):
        self.cdd = self.devices[device_idx]

    def get_current_profile(self):
        profiles = self.cdd.get_profiles()
        if profiles:
            profile = profiles[0]
            profile.connect_sync()
            return profile
        return None

    def remove_profile(self, profile):
        fname = profile.get_filename()
        if not self.cdd.remove_profile_sync(profile):
            print("WARNING: could not remove profile from device", file=sys.stderr)
        elif fname:
            os.remove(fname)

    def _on_profile_added(self, client, profile):
        profile.connect_sync()
        if profile.get_filename() == self.desired_profile_filename:
            self.received_profile = profile

    def new_profile_with_name(self, profile_data, new_name):
        new_profile_path = os.path.join(GLib.get_user_data_dir(), "icc", new_name)
        tmp_profile_path = new_profile_path + "-ggtmp"

        profile_data.save_file(
            Gio.File.new_for_path(tmp_profile_path), Colord.IccSaveFlags.NONE, None
        )

        self.received_profile = None
        self.desired_profile_filename = new_profile_path
        os.rename(tmp_profile_path, new_profile_path)

        ctx = GLib.MainContext().default()
        new_profile = None
        deadline = time.time() + TIMEOUT

        while time.time() < deadline:
            ctx.iteration(False)
            if self.received_profile:
                new_profile = self.received_profile
                self.received_profile = None
                break
            time.sleep(POLL_INTERVAL)

        if not new_profile:
            raise RuntimeError("Profile was not added in time")

        self.cdd.add_profile_sync(Colord.DeviceRelation.HARD, new_profile)
        new_profile.connect_sync()
        return new_profile

    def create_and_set_srgb_profile(self):
        profile = self.cd.find_profile_by_filename_sync("sRGB.icc")
        if not profile:
            return None
        self.cdd.add_profile_sync(Colord.DeviceRelation.HARD, profile)
        profile.connect_sync()
        return profile

    def make_profile_default(self, profile):
        self.cdd.make_profile_default_sync(profile)

    def is_device_enabled(self):
        return self.cdd.get_enabled()

    def set_device_enabled(self, enabled):
        self.cdd.set_enabled_sync(enabled)


# ---------------------------------------------------------------------------
# GUI widgets
# ---------------------------------------------------------------------------
class SliderRow(Gtk.Box):
    """A horizontal row: label | scale | spin-button."""

    def __init__(self, label_text, min_val, max_val, default, step=0.01, digits=2):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_margin_start(4)
        self.set_margin_end(4)

        lbl = Gtk.Label(label=label_text, xalign=0)
        lbl.set_size_request(130, -1)
        self.pack_start(lbl, False, False, 0)

        adj = Gtk.Adjustment(
            value=default,
            lower=min_val,
            upper=max_val,
            step_increment=step,
            page_increment=step * 10,
        )
        self._scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self._scale.set_digits(digits)
        self._scale.set_draw_value(False)
        self._scale.set_hexpand(True)
        self.pack_start(self._scale, True, True, 0)

        self._spin = Gtk.SpinButton(adjustment=adj, climb_rate=step, digits=digits)
        self._spin.set_size_request(80, -1)
        self.pack_start(self._spin, False, False, 0)

    def get_value(self):
        return self._scale.get_value()

    def set_value(self, val):
        self._scale.set_value(val)


class ChannelGroup(Gtk.Box):
    """
    A labelled section for a parameter that supports both a single
    "all channels" slider and individual R/G/B sliders.
    """

    def __init__(self, section_label, min_val, max_val, default, step=0.01, digits=2):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._default = default

        # Header row: bold label + per-channel checkbox
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(4)
        header.set_margin_end(4)

        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(section_label)}</b>")
        header.pack_start(title, True, True, 0)

        self._per_ch = Gtk.CheckButton(label="Per channel")
        self._per_ch.connect("toggled", self._on_toggled)
        header.pack_end(self._per_ch, False, False, 0)

        self.pack_start(header, False, False, 0)

        # Single slider
        self._all_row = SliderRow("All channels", min_val, max_val, default, step, digits)
        self.pack_start(self._all_row, False, False, 0)

        # Per-channel sliders (hidden initially)
        self._r_row = SliderRow("Red", min_val, max_val, default, step, digits)
        self._g_row = SliderRow("Green", min_val, max_val, default, step, digits)
        self._b_row = SliderRow("Blue", min_val, max_val, default, step, digits)
        for row in (self._r_row, self._g_row, self._b_row):
            row.set_no_show_all(True)
            row.hide()
            self.pack_start(row, False, False, 0)

    def _on_toggled(self, btn):
        per_ch = btn.get_active()
        if per_ch:
            v = self._all_row.get_value()
            for row in (self._r_row, self._g_row, self._b_row):
                row.set_value(v)
        self._all_row.set_visible(not per_ch)
        self._r_row.set_visible(per_ch)
        self._g_row.set_visible(per_ch)
        self._b_row.set_visible(per_ch)

    def get_values(self):
        if self._per_ch.get_active():
            return [self._r_row.get_value(), self._g_row.get_value(), self._b_row.get_value()]
        v = self._all_row.get_value()
        return [v, v, v]

    def reset(self):
        self._per_ch.set_active(False)
        self._all_row.set_value(self._default)
        for row in (self._r_row, self._g_row, self._b_row):
            row.set_value(self._default)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class GammaToolWindow(Gtk.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("GNOME Gamma Tool")
        self.set_default_size(620, 580)

        self._mgr = None
        self._build_ui()
        self._init_colord()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        # Scrollable content
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        outer.pack_start(scroll, True, True, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        scroll.add(content)

        # Display selector
        content.pack_start(self._make_display_row(), False, False, 0)
        content.pack_start(Gtk.Separator(), False, False, 0)

        # Parameter groups
        self._gamma_grp = ChannelGroup("Gamma", 0.1, 3.0, 1.0, 0.01, 2)
        content.pack_start(self._gamma_grp, False, False, 0)
        content.pack_start(Gtk.Separator(), False, False, 0)

        content.pack_start(self._make_temp_section(), False, False, 0)
        content.pack_start(Gtk.Separator(), False, False, 0)

        self._contrast_grp = ChannelGroup("Contrast", 0.1, 3.0, 1.0, 0.01, 2)
        content.pack_start(self._contrast_grp, False, False, 0)
        content.pack_start(Gtk.Separator(), False, False, 0)

        self._bmax_grp = ChannelGroup("Max Brightness", 0.0, 1.0, 1.0, 0.01, 2)
        content.pack_start(self._bmax_grp, False, False, 0)

        self._bmin_grp = ChannelGroup("Min Brightness", 0.0, 1.0, 0.0, 0.01, 2)
        content.pack_start(self._bmin_grp, False, False, 0)

        # Status bar
        outer.pack_start(Gtk.Separator(), False, False, 0)
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        status_box.set_margin_start(8)
        status_box.set_margin_end(8)
        status_box.set_margin_top(4)
        status_box.set_margin_bottom(4)
        self._status_lbl = Gtk.Label(label="", xalign=0)
        self._status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        status_box.pack_start(self._status_lbl, True, True, 0)
        outer.pack_start(status_box, False, False, 0)

        # Button bar
        outer.pack_start(Gtk.Separator(), False, False, 0)
        outer.pack_start(self._make_button_bar(), False, False, 0)

        self.show_all()

    def _make_display_row(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(4)
        box.set_margin_end(4)

        lbl = Gtk.Label(label="Display:", xalign=0)
        lbl.set_size_request(130, -1)
        box.pack_start(lbl, False, False, 0)

        self._display_combo = Gtk.ComboBoxText()
        self._display_combo.append_text("Loading…")
        self._display_combo.set_active(0)
        box.pack_start(self._display_combo, True, True, 0)

        return box

    def _make_temp_section(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        hdr = Gtk.Label(xalign=0)
        hdr.set_markup("<b>Color Temperature</b>")
        hdr.set_margin_start(4)
        box.pack_start(hdr, False, False, 0)

        self._temp_row = SliderRow("Temperature (K)", 1000, 12000, 6500, 100, 0)
        box.pack_start(self._temp_row, False, False, 0)

        return box

    def _make_button_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)

        self._remove_btn = Gtk.Button(label="Remove Profile")
        self._remove_btn.connect("clicked", self._on_remove)
        bar.pack_start(self._remove_btn, False, False, 0)

        self._reset_btn = Gtk.Button(label="Reset to Defaults")
        self._reset_btn.connect("clicked", self._on_reset)
        bar.pack_end(self._reset_btn, False, False, 0)

        self._apply_btn = Gtk.Button(label="Apply")
        self._apply_btn.get_style_context().add_class("suggested-action")
        self._apply_btn.connect("clicked", self._on_apply)
        bar.pack_end(self._apply_btn, False, False, 0)

        return bar

    # ------------------------------------------------------------------
    # colord initialisation
    # ------------------------------------------------------------------
    def _init_colord(self):
        if not COLORD_AVAILABLE:
            self._set_status(
                "colord bindings not available. Install gir1.2-colord-1.0 and restart."
            )
            self._apply_btn.set_sensitive(False)
            self._remove_btn.set_sensitive(False)
            return

        try:
            self._mgr = ProfileMgr()
            count = self._mgr.get_device_count()
            self._display_combo.remove_all()
            for name in self._mgr.get_device_names():
                self._display_combo.append_text(name)
            if count:
                self._display_combo.set_active(0)
                self._set_status(f"Found {count} display(s).")
            else:
                self._set_status("No displays found.")
                self._apply_btn.set_sensitive(False)
                self._remove_btn.set_sensitive(False)
        except Exception as exc:
            self._set_status(f"Error initialising colord: {exc}")
            self._apply_btn.set_sensitive(False)
            self._remove_btn.set_sensitive(False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _set_status(self, msg):
        self._status_lbl.set_text(msg)

    def _selected_display(self):
        return self._display_combo.get_active()

    def _build_signature(self, gamma, temperature, contrast, bmin, bmax):
        parts = []
        if gamma != [1.0, 1.0, 1.0]:
            parts.append("g=" + ":".join(f"{v:.2f}" for v in gamma))
        if temperature != 6500:
            parts.append(f"t={temperature}")
        if contrast != [1.0, 1.0, 1.0]:
            parts.append("c=" + ":".join(f"{v:.2f}" for v in contrast))
        if bmin != [0.0, 0.0, 0.0] or bmax != [1.0, 1.0, 1.0]:
            parts.append(
                "r=["
                + ":".join(f"{v:.2f}" for v in bmin)
                + ","
                + ":".join(f"{v:.2f}" for v in bmax)
                + "]"
            )
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Button callbacks
    # ------------------------------------------------------------------
    def _on_apply(self, _btn):
        if not self._mgr:
            return

        idx = self._selected_display()
        if idx < 0:
            self._set_status("No display selected.")
            return

        try:
            gamma = self._gamma_grp.get_values()
            temperature = int(self._temp_row.get_value())
            contrast = self._contrast_grp.get_values()
            bmin = self._bmin_grp.get_values()
            bmax = self._bmax_grp.get_values()
            signature = self._build_signature(gamma, temperature, contrast, bmin, bmax)

            self._mgr.connect_device(idx)

            if not self._mgr.is_device_enabled():
                self._mgr.set_device_enabled(True)

            base_profile = self._mgr.get_current_profile()
            if not base_profile:
                self._set_status("No default profile; using sRGB.")
                base_profile = self._mgr.create_and_set_srgb_profile()

            base_info = base_profile.get_filename() or base_profile.get_id()
            is_our_profile = OUR_PREFIX in base_info

            profile_data, unique_id = build_profile_data(
                base_profile.load_icc(Colord.IccLoadFlags.NONE), signature, gamma, temperature, contrast, bmin, bmax
            )
            new_profile = self._mgr.new_profile_with_name(
                profile_data, OUR_PREFIX + unique_id + ".icc"
            )
            self._mgr.make_profile_default(new_profile)

            if is_our_profile:
                self._mgr.remove_profile(base_profile)

            self._set_status(f"Applied: {new_profile.get_filename()}")
        except Exception as exc:
            self._set_status(f"Error: {exc}")

    def _on_remove(self, _btn):
        if not self._mgr:
            return

        idx = self._selected_display()
        if idx < 0:
            return

        try:
            self._mgr.connect_device(idx)
            profile = self._mgr.get_current_profile()
            if profile:
                info = profile.get_filename() or profile.get_id()
                if OUR_PREFIX in info:
                    self._mgr.remove_profile(profile)
                    self._set_status("Profile removed.")
                    return
            self._set_status("No gnome-gamma-tool profile active.")
        except Exception as exc:
            self._set_status(f"Error: {exc}")

    def _on_reset(self, _btn):
        self._gamma_grp.reset()
        self._contrast_grp.reset()
        self._bmax_grp.reset()
        self._bmin_grp.reset()
        self._temp_row.set_value(6500)
        self._set_status("Reset to defaults. Press Apply to apply changes.")


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------
class GammaToolApp(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="org.gnome.GammaTool",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        win = GammaToolWindow(application=self)
        win.present()


def main():
    if not COLORD_AVAILABLE:
        # Fallback: show a text error and still open the window
        print(
            "Warning: colord GIR bindings not available.\n"
            "On Debian/Ubuntu: sudo apt install gir1.2-colord-1.0",
            file=sys.stderr,
        )
    app = GammaToolApp()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
