#!/usr/bin/env python3
"""GTK3 GUI front-end for gnome-gamma-tool.

All colour / ICC profile logic is reused from gnome-gamma-tool.py instead of
being duplicated here; this module only builds the GTK interface on top of it.
"""

import os
import re
import sys

import gi

gi.require_version("Gtk", "3.0")

try:
    gi.require_version("Colord", "1.0")
    _colord_version_ok = True
except ValueError:
    _colord_version_ok = False

from gi.repository import GLib, Gio, Gtk, Pango

if _colord_version_ok:
    try:
        from gi.repository import Colord  # noqa: F401  (availability probe)

        COLORD_AVAILABLE = True
    except ImportError:
        COLORD_AVAILABLE = False
else:
    COLORD_AVAILABLE = False

# The CLI module runs gi.require_version("Colord", ...) and exit(1)s at import
# time when the bindings are missing, so only import it once we know they exist.
ggt = None
if COLORD_AVAILABLE:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ggt = __import__("gnome-gamma-tool")

OUR_PREFIX = ggt.OUR_PREFIX if ggt else "gnome-gamma-tool-"
REVERT_SECONDS = 10


# ---------------------------------------------------------------------------
# Signature parsing (inverse of ggt.generate_signature)
# ---------------------------------------------------------------------------
def _triple(text):
    parts = text.split(":")
    if len(parts) == 1:
        return [float(parts[0])] * 3
    return [float(p) for p in parts]


def parse_signature(sig):
    """Parse a 'gamma-tool:' signature back into control values.

    Returns (gamma, temperature, contrast, bmin, bmax). Unknown / "neutral"
    signatures fall back to neutral defaults.
    """
    gamma = [1.0, 1.0, 1.0]
    temperature = 6500
    contrast = [1.0, 1.0, 1.0]
    bmin = [0.0, 0.0, 0.0]
    bmax = [1.0, 1.0, 1.0]

    if not sig or sig == "neutral":
        return gamma, temperature, contrast, bmin, bmax

    # Brightness range token r=[min,max] contains a comma, parse it out first.
    mr = re.search(r"r=\[([^,]+),([^\]]+)\]", sig)
    if mr:
        try:
            bmin = _triple(mr.group(1))
            bmax = _triple(mr.group(2))
        except ValueError:
            pass
        sig = sig[: mr.start()] + sig[mr.end() :]

    for tok in sig.split():
        try:
            if tok.startswith("g="):
                gamma = _triple(tok[2:])
            elif tok.startswith("t="):
                temperature = int(float(tok[2:]))
            elif tok.startswith("c="):
                contrast = _triple(tok[2:])
        except ValueError:
            pass

    return gamma, temperature, contrast, bmin, bmax


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
            row.show_all()  # need to show all children first, then hide the parent
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

    def set_values(self, vals):
        if len(set(vals)) == 1:
            self._per_ch.set_active(False)
            self._all_row.set_value(vals[0])
        else:
            self._per_ch.set_active(True)
            self._r_row.set_value(vals[0])
            self._g_row.set_value(vals[1])
            self._b_row.set_value(vals[2])

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

        # colord's device proxy gets poisoned once its profile objects are
        # connect_sync()'d (get_profiles() then permanently returns empty for
        # that proxy), so we never reuse a manager: every user action builds a
        # fresh ProfileMgr and does a single read, exactly like the CLI does
        # per-process.
        self._ready = False
        self._device_count = 0
        self._loading = True  # suppress combo "changed" during population
        self._build_ui()
        self._init_colord()

    def _new_manager(self, device_idx):
        """A fresh, un-poisoned ProfileMgr connected to the given display."""
        mgr = ggt.ProfileMgr()
        mgr.connect(device_idx)
        return mgr

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
        self._display_combo.connect("changed", self._on_display_changed)
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
            self._loading = False
            return

        try:
            # Probe for the display list only; enumerating devices connects the
            # device proxies (harmless) but never their profile proxies, so this
            # throwaway manager stays clean. We discard it immediately.
            probe = ggt.ProfileMgr()
            self._device_count = probe.get_device_count()
            names = probe.get_device_names()
            self._display_combo.remove_all()
            for name in names:
                self._display_combo.append_text(name)
            if self._device_count:
                self._ready = True
                self._display_combo.set_active(0)
                self._set_status(f"Found {self._device_count} display(s).")
                self._loading = False
                self._load_current_into_controls()
            else:
                self._set_status("No displays found.")
                self._apply_btn.set_sensitive(False)
                self._remove_btn.set_sensitive(False)
                self._loading = False
        except Exception as exc:
            self._set_status(f"Error initialising colord: {exc}")
            self._apply_btn.set_sensitive(False)
            self._remove_btn.set_sensitive(False)
            self._loading = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _set_status(self, msg):
        self._status_lbl.set_text(msg)

    def _selected_display(self):
        return self._display_combo.get_active()

    def _load_current_into_controls(self):
        """Read the active profile's signature and prime the controls with it."""
        idx = self._selected_display()
        if idx < 0 or not self._ready:
            return

        title = None
        try:
            mgr = self._new_manager(idx)
            profile = mgr.get_current_profile()
            if profile:
                title = profile.get_title()
        except Exception:
            title = None

        sig = None
        if title and title.startswith("gamma-tool:"):
            sig = title.split(":", 1)[1].strip()

        gamma, temperature, contrast, bmin, bmax = parse_signature(sig)
        self._gamma_grp.set_values(gamma)
        self._contrast_grp.set_values(contrast)
        self._bmin_grp.set_values(bmin)
        self._bmax_grp.set_values(bmax)
        self._temp_row.set_value(temperature)

    def _on_display_changed(self, _combo):
        if self._loading:
            return
        self._load_current_into_controls()

    # ------------------------------------------------------------------
    # Revert countdown
    # ------------------------------------------------------------------
    def _confirm_or_revert(self, mgr, base_profile, new_profile, is_our_profile):
        """Ask the user to keep the just-applied profile, auto-reverting on timeout.

        Mirrors the CLI's ask_new_settings_ok() safety net so a bad setting can
        never leave the screen stuck.
        """
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Keep these display settings?",
        )
        dlg.add_button("Revert", Gtk.ResponseType.CANCEL)
        keep_btn = dlg.add_button("Keep", Gtk.ResponseType.OK)
        keep_btn.get_style_context().add_class("suggested-action")

        remaining = {"n": REVERT_SECONDS}

        def show_remaining():
            dlg.format_secondary_text(
                f"Reverting automatically in {remaining['n']} second(s)…"
            )

        def tick():
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                dlg.response(Gtk.ResponseType.CANCEL)
                return False
            show_remaining()
            return True

        show_remaining()
        source_id = GLib.timeout_add_seconds(1, tick)
        response = dlg.run()
        GLib.source_remove(source_id)
        dlg.destroy()

        if response == Gtk.ResponseType.OK:
            try:
                if is_our_profile:
                    mgr.remove_profile(base_profile)
            except Exception:
                pass
            self._set_status("Settings kept.")
            self._load_current_into_controls()
        else:
            try:
                mgr.make_profile_default(base_profile)
                mgr.remove_profile(new_profile)
                mgr.make_profile_default(base_profile)
            except Exception as exc:
                self._set_status(f"Revert failed: {exc}")
                return
            self._set_status("Reverted to previous settings.")
            self._load_current_into_controls()

    # ------------------------------------------------------------------
    # Button callbacks
    # ------------------------------------------------------------------
    def _on_apply(self, _btn):
        if not self._ready:
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

            mgr = self._new_manager(idx)

            if not mgr.is_device_enabled():
                mgr.set_device_enabled(True)

            base_profile = mgr.get_current_profile()
            if not base_profile:
                self._set_status("No default profile; using sRGB.")
                base_profile = mgr.create_and_set_sRGB_profile()

            base_info = base_profile.get_filename() or base_profile.get_id()
            is_our_profile = OUR_PREFIX in base_info

            profile_data, unique_id = ggt.create_profile_data(
                base_profile.load_icc(0), gamma, temperature, contrast, bmin, bmax
            )
            new_profile = mgr.new_profile_with_name(
                profile_data, OUR_PREFIX + unique_id + ".icc"
            )
            mgr.make_profile_default(new_profile)
        except Exception as exc:
            self._set_status(f"Error: {exc}")
            return

        self._confirm_or_revert(mgr, base_profile, new_profile, is_our_profile)

    def _on_remove(self, _btn):
        if not self._ready:
            return

        idx = self._selected_display()
        if idx < 0:
            return

        try:
            mgr = self._new_manager(idx)
            profile = mgr.get_current_profile()
            if profile:
                info = profile.get_filename() or profile.get_id()
                if OUR_PREFIX in info:
                    mgr.remove_profile(profile)
                    self._set_status("Profile removed.")
                    self._load_current_into_controls()
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
