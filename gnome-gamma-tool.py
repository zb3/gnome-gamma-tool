#!/usr/bin/env python3

import os
import time
import gi
import sys
import tty
import uuid
import fcntl
import atexit
import select
import termios
import argparse

try:
    gi.require_version("Colord", "1.0")
except ValueError:
    print("Colord bindings not available!", file=sys.stderr)
    print("On debian/ubuntu based systems, you might need to run this first:", file=sys.stderr)
    print("sudo apt install gir1.2-colord-1.0")
    print("If you're using OpenSUSE, you might need to run this instead:", file=sys.stderr)
    print("sudo zypper install typelib-1_0-Colord-1_0")
    exit(1)

from gi.repository import GObject, GLib, Colord, Gio

N_SAMPLES = 256
OUR_PREFIX = "gnome-gamma-tool-"
INSTANCE_LOCK_FILE = "/tmp/.gnome-gamma-tool.lock"
TIMEOUT = 4
POLL_INTERVAL = 0.01


def linear_map(x, smin, smax, dmin, dmax):
    y = (x - smin) / (smax - smin) * (dmax - dmin) + dmin
    return y


def generate_vcgt(gamma, color_temperature, contrast, bmin, bmax):
    gamma_factor = [1 / float(ga) for ga in gamma]
    temp_color = Colord.ColorRGB()
    vcgt = [None] * N_SAMPLES

    Colord.color_get_blackbody_rgb_full(
        color_temperature, temp_color, Colord.ColorBlackbodyFlags.USE_PLANCKIAN
    )

    for i in range(N_SAMPLES):
        c = Colord.ColorRGB.new()

        # gamma and temperature
        c.R = temp_color.R * ((i / (N_SAMPLES - 1)) ** gamma_factor[0])
        c.G = temp_color.G * ((i / (N_SAMPLES - 1)) ** gamma_factor[1])
        c.B = temp_color.B * ((i / (N_SAMPLES - 1)) ** gamma_factor[2])

        # contrast
        c.R = max(0, min(contrast[0] * (c.R - 0.5) + 0.5, 1))
        c.G = max(0, min(contrast[1] * (c.G - 0.5) + 0.5, 1))
        c.B = max(0, min(contrast[2] * (c.B - 0.5) + 0.5, 1))

        # brightness map
        c.R = linear_map(c.R, 0, 1, bmin[0], bmax[0])
        c.G = linear_map(c.G, 0, 1, bmin[1], bmax[1])
        c.B = linear_map(c.B, 0, 1, bmin[2], bmax[2])

        vcgt[i] = c

    return vcgt


def generate_signature(gamma, temperature, contrast, bmin, bmax):
    arg_sig_parts = []

    def fmt(val):
        # Compress to a single value if all channels are identical (e.g. "0.8" instead of "0.8:0.8:0.8")
        if len(set(val)) == 1:
            return f"{val[0]:g}"
        return ":".join(f"{v:g}" for v in val)

    if any(g != 1.0 for g in gamma):
        arg_sig_parts.append(f"g={fmt(gamma)}")

    if temperature != 6500:
        arg_sig_parts.append(f"t={temperature}")

    if any(c != 1.0 for c in contrast):
        arg_sig_parts.append(f"c={fmt(contrast)}")

    if any(b != 0.0 for b in bmin) or any(b != 1.0 for b in bmax):
        arg_sig_parts.append(f"r=[{fmt(bmin)},{fmt(bmax)}]")

    return " ".join(arg_sig_parts) if arg_sig_parts else "neutral"


def create_profile_data(profile_data, gamma, temperature, contrast, bmin, bmax):
    if not profile_data:
        profile_data = Colord.Icc.new()
        profile_data.create_default()

    signature = generate_signature(gamma, temperature, contrast, bmin, bmax)

    title = f"gamma-tool: {signature}"
    profile_data.set_description("", title)
    profile_data.set_model("", title)

    unique_id = str(uuid.uuid4())
    profile_data.add_metadata("uuid", unique_id)  # to make checksum unique

    vcgt = generate_vcgt(gamma, temperature, contrast, bmin, bmax)
    profile_data.set_vcgt(vcgt)

    return profile_data, unique_id


def float_per_color(arg, fit=False):
    if ":" not in arg:
        arg = f"{arg}:{arg}:{arg}"

    val = list(map(float, arg.split(":")))

    if fit and (maxval := max(val)) > 1:  # the walrus operator says hello
        val = list(map(lambda x: x / maxval, val))

    return val


def parse_args():
    parser = argparse.ArgumentParser(
        epilog=f"""for gamma, contrast and brightness levels you can use the form R:G:B to set these values for individual channels.

use the "--arg=value" form if you get the "expected one argument" error

examples:
{sys.argv[0]} -g 0.8
{sys.argv[0]} -g 0.8:0.8:0.8""",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "-d", "--display", help="display index (0 is default)", type=int, default=0
    )

    parser.add_argument(
        "-a", "--all-displays", help="apply to all displays", action="store_true"
    )

    parser.add_argument(
        "-g", "--gamma", help="target gamma correction (1 is neutral)", default="1"
    )
    parser.add_argument(
        "-t",
        "--temperature",
        help="target color temperature (6500 is neutral)",
        type=int,
        default=6500,
    )
    parser.add_argument(
        "-c",
        "--contrast",
        help="target contrast (1 is neutral, -1 to invert)",
        default="1",
    )
    parser.add_argument(
        "-b",
        "--brightness",
        help="maximum output brightness (1 is neutral)",
        default="1",
    )
    parser.add_argument(
        "-bm",
        "--min-brightness",
        help="minimum output brightness, (0 is neutral)",
        default="0",
    )
    parser.add_argument(
        "-r",
        "--remove",
        help="remove the profile created by gnome-gamma-tool",
        action="store_true",
    )
    parser.add_argument(
        "-y", "--yes", help="don't ask whether new settings are ok", action="store_true"
    )

    parser.add_argument(
        "-o",
        "--out-file",
        help="create an ICC file without applying it",
        default=None,
    )

    parser.add_argument(
        "-i",
        "--in-file",
        help="use this ICC file as the base profile (usable only with -o/--out-file)",
        default=None,
    )

    if len(sys.argv) < 2:
        parser.print_help()
        sys.exit(2)

    args = parser.parse_args()

    args.gamma = float_per_color(args.gamma)
    args.contrast = float_per_color(args.contrast)
    args.min_brightness = float_per_color(args.min_brightness, fit=True)
    args.brightness = float_per_color(args.brightness, fit=True)

    return args


def ensure_not_running():
    fp = os.open(INSTANCE_LOCK_FILE, os.O_WRONLY | os.O_CREAT)

    try:
        fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        return False

    atexit.register(lambda: os.remove(INSTANCE_LOCK_FILE))

    return True


def ask_new_settings_ok():
    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    ret = False
    print("")  # reserve space

    try:
        seconds_to_wait = 10
        for s in range(seconds_to_wait):
            sys.stdout.write("\033[1A\033[2K\r")  # move up and clear the line
            sys.stdout.write(
                "Settings will revert in %d seconds...\n" % (seconds_to_wait - s)
            )
            sys.stdout.write("Keep these changes? [y/N] ")
            sys.stdout.flush()
            i, o, e = select.select([sys.stdin], [], [], 1)

            if i:
                ret = sys.stdin.read(1) == "y"
                break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, old_attr)

    print()

    return ret


class ProfileMgr:
    def __init__(self):
        self.cd = Colord.Client()
        self.cd.connect_sync()
        self.devices = self._fetch_display_devices()
        self.desired_profile_filename = None
        self.received_profile = None
        self.cdd = None
        super(GObject.Object, self.cd).connect('profile-added', self.on_profile_added)

    def _fetch_display_devices(self):
        all_devices = self.cd.get_devices_sync()
        display_devices = []

        # we can't retrieve the kind without connecting first
        for device in all_devices:
            device.connect_sync()
            if device.get_kind() == Colord.DeviceKind.DISPLAY:
                display_devices.append(device)

        return display_devices

    def get_display_devices(self):
        return self.devices

    def get_device_names(self):
        names = []
        for i, device in enumerate(self.devices):
            name = ' '.join(filter(None, [device.get_vendor(), device.get_model()])) or f"Display {i}"
            names.append(name)
        return names

    def get_device_count(self):
        return len(self.devices)

    def connect(self, device_idx):
        self.cdd = self.devices[device_idx]

    def get_current_profile(self):
        profiles = self.cdd.get_profiles()
        profile = None

        if profiles:
            profile = profiles[0]
            profile.connect_sync()

        return profile

    def remove_profile(self, profile):
        fname = profile.get_filename()

        if not self.cdd.remove_profile_sync(profile):
            print('WARNING: could not remove profile from device, aborting')

        elif fname:
            os.remove(fname)

    def clone_profile_data(self, profile):
        return profile.load_icc(0)

    def on_profile_added(self, client, profile):
        profile.connect_sync()
        if profile.get_filename() == self.desired_profile_filename:
            self.received_profile = profile

    def new_profile_with_name(self, profile_data, new_name):
        # import_profile_sync turned out to be unreliable as the newly
        # written profile is sometimes not detected when written directly,
        # so we first write to a temp file, then rename it

        new_profile_path = os.path.join(GLib.get_user_data_dir(), 'icc', new_name)
        tmp_profile_path = new_profile_path + '-ggtmp'

        profile_data.save_file(
            Gio.File.new_for_path(tmp_profile_path), Colord.IccSaveFlags.NONE, None
        )

        self.received_profile = None
        self.desired_profile_filename = new_profile_path

        # at this point the profile "should" be detected, but it sometimes isn't
        # and we need to rename the file to make it work
        os.rename(tmp_profile_path, new_profile_path)

        # as the profile is not immediately added, we need to catch the signal
        # the easiest way to do so seems to be to manually iterate the context

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
            raise Exception('profile was not added in time')

        self.cdd.add_profile_sync(Colord.DeviceRelation.HARD, new_profile)
        new_profile.connect_sync()

        return new_profile

    def create_and_set_sRGB_profile(self):
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

def main():
    args = parse_args()

    if args.yes and not ensure_not_running():
        exit("gnome-gamma-tool is already running")

    if args.out_file:
        print("Creating profile...")
        profile_data = None
        if args.in_file:
            print(f"Using {args.in_file} as base profile...")
            icc_file = Gio.File.new_for_path(args.in_file)
            profile_data = Colord.Icc.new()
            Colord.Icc.load_file(profile_data, icc_file, Colord.IccLoadFlags.ALL, None)

        profile_data, _ = create_profile_data(
            profile_data,
            args.gamma,
            args.temperature,
            args.contrast,
            args.min_brightness,
            args.brightness
        )

        profile_data.save_file(
            Gio.File.new_for_path(args.out_file), Colord.IccSaveFlags.NONE, None
        )
        print(f"Profile saved to {args.out_file}")
        return

    mgr = ProfileMgr()

    display_count = mgr.get_device_count()
    if args.display >= display_count:
        exit(f"No such display, found {display_count} displays.")

    for display_idx in range(0, display_count if args.all_displays else 1):
        mgr.connect(display_idx if args.all_displays else args.display)

        if not mgr.is_device_enabled():
            # but 'd we have the default edid profile?

            print("Enabling color management for device")
            mgr.set_device_enabled(True)

        # we get the default one, even if some ggt profiles exist
        # this is to that you can have more than one ggt profile
        base_profile = mgr.get_current_profile()

        if not base_profile:
            print("No default profile, using sRGB")
            base_profile = mgr.create_and_set_sRGB_profile()

        base_profile_info = base_profile.get_filename() or base_profile.get_id()
        our_profile = OUR_PREFIX in base_profile_info
        print("Current profile is", base_profile_info)

        if args.remove:
            if our_profile:
                print("Removing profile")
                mgr.remove_profile(base_profile)

        else:
            profile_data, unique_id = create_profile_data(
                base_profile.load_icc(0),
                args.gamma,
                args.temperature,
                args.contrast,
                args.min_brightness,
                args.brightness
            )

            new_profile = mgr.new_profile_with_name(profile_data, OUR_PREFIX + unique_id + '.icc')
            print("New profile is", new_profile.get_filename())

            mgr.make_profile_default(new_profile)

            if sys.stdout.isatty() and not args.yes and not ask_new_settings_ok():
                print("Reverting settings")
                mgr.make_profile_default(base_profile)
                mgr.remove_profile(new_profile)
                mgr.make_profile_default(base_profile)

            elif our_profile:
                print("Removing old profile")
                mgr.remove_profile(base_profile)


if __name__ == "__main__":
    main()
