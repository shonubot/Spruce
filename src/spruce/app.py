#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Spruce — GNOME Cleaner

from __future__ import annotations

import os
import math
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango, PangoCairo  # type: ignore

# Optional cairo import (some distros split the Python binding)
try:
    import cairo  # type: ignore
except Exception:
    cairo = None  # chart will show a text fallback

APP_ID = "io.github.shonubot.Spruce"
IS_FLATPAK = os.environ.get("FLATPAK_ID") == APP_ID


# ─────────────────────────── helpers ─────────────────────────


def _find_ui() -> str:
    """Locate ui/window.ui both in dev trees and in Flatpak installs."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "ui" / "window.ui", # repo: src/spruce/ -> ../../ui/window.ui
        Path.cwd() / "ui" / "window.ui", # running from repo root
        Path("/app/share/io.github.shonubot.Spruce/ui/window.ui"), # Flatpak
        Path("/app/share/spruce/ui/window.ui"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    # Last-ditch relative path so dev runs don’t crash outright
    return str(Path("ui") / "window.ui")


def human_size(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024.0
    return f"{f:.1f}EiB"


def _gio_fs_usage(path: Path) -> Tuple[int, int, int] | None:
    """
    Try to read filesystem totals (size, free) using GIO. Returns (total, used, free)
    or None if the query fails.
    """
    try:
        gfile = Gio.File.new_for_path(str(path))
        info = gfile.query_filesystem_info("filesystem::size,filesystem::free", None)
        total = int(info.get_attribute_uint64("filesystem::size"))
        free = int(info.get_attribute_uint64("filesystem::free"))
        used = max(0, total - free)
        # Basic sanity: ignore obviously bogus answers
        if total > 0 and free >= 0 and used >= 0:
            return total, used, free
    except Exception:
        pass
    return None


def _host_view(path: Path) -> Path:
    """
    In Flatpak, map a path to the host filesystem when possible
    (e.g. /home/USER -> /run/host/home/USER) so disk stats are correct.
    """
    if not IS_FLATPAK:
        return path
    try:
        rel = path.resolve().relative_to("/") # e.g. 'home/USER'
        host = Path("/run/host") / rel # e.g. /run/host/home/USER
        if host.exists():
            return host
    except Exception:
        pass
    return path


def disk_usage_home() -> Tuple[int, int, int]:
    """
    Correct disk stats even in a Flatpak sandbox.
    Order of preference:
      1) /run/host/home/$USER
      2) /run/host (host root)
      3) sandbox home (~/.var/app/<app>)
    """
    candidates: list[Path] = []
    if IS_FLATPAK:
        # Best: the host's actual home
        try:
            candidates.append(_host_view(Path.home()))
            candidates.append(Path("/run/host")) # fall back to host root if needed
        except Exception:
            candidates.append(Path("/run/host"))
    # Always include sandbox home as last resort
    candidates.append(Path.home())

    # First try GIO (more accurate on bind mounts), then fall back to shutil
    for p in candidates:
        ans = _gio_fs_usage(p)
        if ans:
            return ans
        try:
            total, used, free = shutil.disk_usage(str(p))
            total, used, free = int(total), int(used), int(free)
            if total > 0:
                return total, used, free
        except Exception:
            continue

    # Should never hit this; keep UI alive
    return 1, 0, 1


def xdg_cache() -> Path:
    # In Flatpak this is usually ~/.var/app/<app>/cache
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))


def list_apt_autoremove() -> List[str]:
    """Host-only: parse apt-get --dry-run autoremove for packages."""
    try:
        out = subprocess.check_output(
            ["bash", "-lc", r"apt-get --dry-run autoremove | grep -Po '^Remv \K[^ ]+'"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
        except Exception:
            pass
    return []

def list_flatpak_unused() -> List[str]:
    """
    List runtimes and extensions that can be uninstalled.
    Returns a list of `id/arch/branch` strings.
    """
    unused = []
    # Check for user-level unused packages
    try:
        # We use GLib.spawn_sync to synchronously get the output.
        _res, out, _err, _status = GLib.spawn_sync(
            None,
            ["flatpak", "uninstall", "--unused", "--dry-run", "--columns=ref"],
            None,
            GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_PIPE,
            None,
        )
        if out is not None:
            # Decode output and split into lines.
            lines = out.decode('utf-8').strip().splitlines()
            # Filter out the header line which usually says 'Ref'
            unused.extend([line for line in lines if line and not line.startswith('Ref')])
    except GLib.Error:
        pass
    except Exception:
        pass
    
    # Check for system-level unused packages (requires elevated privileges)
    try:
        _res, out, _err, _status = GLib.spawn_sync(
            None,
            ["pkexec", "flatpak", "uninstall", "--unused", "--dry-run", "--system", "--columns=ref"],
            None,
            GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_PIPE,
            None,
        )
        if out is not None:
            lines = out.decode('utf-8').strip().splitlines()
            unused.extend([line for line in lines if line and not line.startswith('Ref')])
    except GLib.Error:
        pass
    except Exception:
        pass

    return unused


def run_combined_autoremove_async(on_done) -> None:
    """
    Launch a single pkexec command to remove both apt and Flatpak unused packages.
    """
    # Create a single command string with a shell to run both
    cmd = "apt autoremove -y; flatpak uninstall --unused --system -y"
    try:
        pid, _, _ = GLib.spawn_async(
            ["pkexec", "sh", "-c", cmd],
            flags=GLib.SpawnFlags.SEARCH_PATH,
        )
        def _after(_pid, _cond):
            GLib.timeout_add_seconds(2, on_done)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, _after)
    except Exception:
        GLib.timeout_add_seconds(2, on_done)

def run_flatpak_autoremove_async(on_done) -> None:
    """Launch flatpak uninstall --unused -y, then call on_done() shortly after."""
    try:
        # Use --system to handle system-wide unused packages
        pid, _, _ = GLib.spawn_async(
            ["pkexec", "flatpak", "uninstall", "--unused", "--system", "-y"],
            flags=GLib.SpawnFlags.SEARCH_PATH,
        )
        def _after(_pid, _cond):
            GLib.timeout_add_seconds(2, on_done)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, _after)
    except Exception:
        GLib.timeout_add_seconds(2, on_done)


def dir_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for root, _dirs, files in os.walk(path, onerror=lambda *_: None):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except Exception:
                    pass
    elif path.exists():
        try:
            total += path.stat().st_size
        except Exception:
            pass
    return total


# ─────────────────────── main window class ───────────────────────

@Gtk.Template(filename=_find_ui())
class SpruceWindow(Adw.ApplicationWindow):
    __gtype_name__ = "SpruceWindow"

    pie_chart: Gtk.DrawingArea = Gtk.Template.Child("pie_chart")
    clear_btn: Gtk.Button = Gtk.Template.Child("clear_btn")
    options_btn: Gtk.Button = Gtk.Template.Child("options_btn")
    # Using the existing UI components from the blueprint.
    pkg_list: Gtk.Label = Gtk.Template.Child("pkg_list")
    remove_btn: Gtk.Button = Gtk.Template.Child("remove_btn")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Chart setup (DrawingArea declared in Blueprint)
        self.pie_chart.set_hexpand(True)
        self.pie_chart.set_vexpand(True)
        self.pie_chart.set_content_width(360)
        self.pie_chart.set_content_height(260)
        self.pie_chart.set_draw_func(self._draw_chart, None)

        # Wire buttons
        self.clear_btn.connect("clicked", self._on_clear_clicked)
        self.options_btn.connect("clicked", self._on_options_clicked)
        self.remove_btn.connect("clicked", self._on_remove_clicked)
        self.timeout_source = None

        # Options state (defaults)
        self._opts = {
            "thumbs": True,
            "webkit": True,
            "fontconf": True,
            "mesa": True,
            "sweep": True, # enabled by default
        }

        # Instance variable to hold the toast message so we can dismiss it later
        self._current_toast = None

        # Initial UI refresh
        self._refresh_autoremove_label()
        self.pie_chart.queue_draw()

    # ─────────────── unified cleanup card ───────────────

    def _refresh_autoremove_label(self):
        # This function is now also the callback for the auto-refresh loop.
        if self.timeout_source:
            GLib.source_remove(self.timeout_source)
            self.timeout_source = None

        if IS_FLATPAK:
            pkgs = list_flatpak_unused()
            if pkgs:
                self.pkg_list.set_text(" ".join(pkgs))
                self.remove_btn.set_sensitive(True)
            else:
                self.pkg_list.set_text("No unused runtimes or extensions to remove.")
                self.remove_btn.set_sensitive(False)
        else:
            apt_pkgs = list_apt_autoremove()
            flatpak_pkgs = list_flatpak_unused()

            all_pkgs = []
            if apt_pkgs:
                all_pkgs.append("APT: " + " ".join(apt_pkgs))
            if flatpak_pkgs:
                all_pkgs.append("Flatpak: " + " ".join(flatpak_pkgs))

            if all_pkgs:
                self.pkg_list.set_text(" | ".join(all_pkgs))
                self.remove_btn.set_sensitive(True)
                # If there are still packages, schedule another refresh in 2 seconds.
                self.timeout_source = GLib.timeout_add_seconds(2, self._refresh_autoremove_label)
            else:
                self.pkg_list.set_text("No packages or unused Flatpak runtimes available to remove.")
                self.remove_btn.set_sensitive(False)

        return GLib.SOURCE_REMOVE

    def _on_remove_clicked(self, _btn):
        # This function now handles both apt and Flatpak removal with a single call.
        if IS_FLATPAK:
            run_flatpak_autoremove_async(self._refresh_autoremove_label)
        else:
            run_combined_autoremove_async(self._refresh_autoremove_label)

    # ─────────────── clear temp / options ───────────────

    def _on_clear_clicked(self, _btn):
        # We start the file scan in a new thread only if the "sweep" option is on.
        if self._opts["sweep"]:
            # Store a reference to the toast so we can close it later
            self._current_toast = self._toast("Scanning cache directories...")
            GLib.Thread.new("cache_scanner", self._scan_cache_in_thread)
        else:
            # If sweep is off, just perform the instant clears on the main thread.
            removed = self._perform_instant_clears()
            if removed:
                self._toast("Selected caches cleared")
                self.pie_chart.queue_draw()

    def _perform_instant_clears(self):
        """Helper function to run the quick-clearing options."""
        def rm_rf(p: Path) -> bool:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink(missing_ok=True)
                return True
            except Exception:
                return False

        removed = False
        cache = xdg_cache()
        if self._opts["thumbs"]:
            removed |= rm_rf(cache / "thumbnails")
        if self._opts["webkit"]:
            for name in ("WebKitGTK", "webkitgtk"):
                removed |= rm_rf(cache / name)
        if self._opts["fontconf"]:
            removed |= rm_rf(cache / "fontconfig")
        if self._opts["mesa"]:
            removed |= rm_rf(cache / "mesa_shader_cache")
        return removed

    def _on_options_clicked(self, _btn):
        win = Adw.PreferencesWindow(transient_for=self, modal=True, title="Preferences")
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="What to clear when you press “Clear temp”")
        page.add(group)

        def add_switch(title, subtitle, key):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=self._opts[key])
            row.connect("notify::active",
                        lambda r, *_: self._opts.__setitem__(key, r.get_active()))
            group.add(row)

        add_switch("Thumbnail cache", "~/.cache/thumbnails", "thumbs")
        add_switch("WebKitGTK caches", "~/.cache/WebKitGTK or ~/.cache/webkitgtk", "webkit")
        add_switch("Fontconfig cache", "~/.cache/fontconfig", "fontconf")
        add_switch("Mesa shader cache", "~/.cache/mesa_shader_cache", "mesa")

        g2 = Adw.PreferencesGroup(title="General")
        page.add(g2)
        row = Adw.SwitchRow(
            title="General cache sweep",
            subtitle="Pick large items in ~/.cache to remove",
            active=self._opts["sweep"],
        )
        row.connect("notify::active",
                    lambda r, *_: self._opts.__setitem__("sweep", r.get_active()))
        g2.add(row)

        # Headerbar
        hb = Adw.HeaderBar()

        # Build container ONCE to avoid double-parent warnings
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(hb)
        box.append(page)
        win.set_content(box)

        win.present()

    # ─────────────── cache sweep dialog ───────────────

    def _cache_roots(self) -> list[tuple[Path, bool]]:
        """
        Return [(path, can_delete)] roots to list in the sweep dialog.
        Includes the app cache (writable), the host's cache, and other Flatpak app caches.
        """
        roots: list[tuple[Path, bool]] = []
        unique_paths = set()

        # Add the main cache directory
        main_cache = xdg_cache()
        if main_cache.is_dir():
            unique_paths.add(main_cache)

        if IS_FLATPAK:
            # Check for the host's cache directory (for Flatpak version)
            host_cache = _host_view(Path.home()) / ".cache"
            if host_cache.is_dir():
                unique_paths.add(host_cache)

            # Add cache directories for other Flatpak apps
            flatpak_app_dir = Path.home() / ".var" / "app"
            if flatpak_app_dir.is_dir():
                for app_dir in flatpak_app_dir.iterdir():
                    app_cache_dir = app_dir / "cache"
                    if app_cache_dir.is_dir():
                        unique_paths.add(app_cache_dir)

        # Now convert the set of unique paths into the desired list format
        for p in unique_paths:
            can_write = os.access(p, os.W_OK | os.X_OK)
            roots.append((p, bool(can_write)))

        return roots

    # This is the corrected method. It no longer takes the `_thread` parameter.
    def _scan_cache_in_thread(self):
        """
        Scan cache directories for large files and pass the results to the UI thread.
        This function runs in a background thread and does not block the UI.
        """
        entries: list[tuple[Path, int, bool]] = []
        for root, can_delete in self._cache_roots():
            try:
                for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                    try:
                        # The slow, blocking work is now done here, in the background.
                        size = dir_size(child)
                        entries.append((child, size, can_delete))
                    except Exception:
                        pass
            except Exception:
                pass

        # When done, pass the results back to the main thread to show the dialog.
        GLib.idle_add(self._show_sweep_dialog, entries)
        # Note: The return value of a thread worker function is generally ignored.
        return None

    def _show_sweep_dialog(self, entries):
        """
        This function is now responsible for building and displaying the dialog
        once the file scanning is complete. It is called from the main thread.
        """
        # Close the "Scanning..." toast message
        if self._current_toast:
            self._current_toast.close()
            self._current_toast = None

        dlg = Adw.Dialog.new()
        dlg.set_title("Cache sweep")
        dlg.present(self) # attach to parent before sizing/content
        dlg.set_content_width(720)
        dlg.set_content_height(520)

        header = Adw.HeaderBar()

        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        v.set_margin_top(12)
        v.set_margin_bottom(12)
        v.set_margin_start(12)
        v.set_margin_end(12)


        base_lines = []
        for root, can_delete in self._cache_roots():
            tag = "(sandbox)" if root == xdg_cache() else ("(read-only)" if not can_delete else "")
            base_lines.append(f"{root} {tag}".rstrip())
        title = Gtk.Label(label="Select the cache files to remove:", xalign=0)
        title.add_css_class("title-4")
        v.append(title)

        sc = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        sc.set_child(listbox)
        v.append(sc)

        toggles: List[Gtk.Switch] = []
        paths: List[Path] = []
        deletable: List[bool] = []

        for p, sz, can_delete in sorted(entries, key=lambda t: t[1], reverse=True)[:120]:
            row = Adw.ActionRow(title=p.name, subtitle=f"{p} — {human_size(sz)}")
            sw = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=can_delete)
            row.add_suffix(sw)
            listbox.append(row)
            toggles.append(sw)
            paths.append(p)
            deletable.append(can_delete)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        sel_all = Gtk.CheckButton(label="Select all")
        rm_btn = Gtk.Button(label="Remove selected", sensitive=False)
        actions.append(sel_all)
        actions.append(rm_btn)
        v.append(actions)

        def update_btn(*_a):
            rm_btn.set_sensitive(any(s.get_active() and s.get_sensitive() for s in toggles))

        for s in toggles:
            s.connect("notify::active", update_btn)

        def _set_all(active: bool):
            for s in toggles:
                if s.get_sensitive():
                    s.set_active(active)
            update_btn()

        sel_all.connect("toggled", lambda b: _set_all(b.get_active()))

        def do_rm(_b):
            removed = 0
            for sw, p, can_delete in zip(toggles, paths, deletable):
                if not can_delete:
                    continue
                if sw.get_active() and p.exists():
                    try:
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            p.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass
            if removed:
                self._toast(f"Removed {removed} item(s)")
                self.pie_chart.queue_draw()
            dlg.close()

        rm_btn.connect("clicked", do_rm)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.append(header)
        body.append(v)
        dlg.set_child(body)

    # ─────────────── pie chart ───────────────

    def _draw_chart(self, _area, cr, w: int, h: int, _data):
        if cairo is None:
            layout = PangoCairo.create_layout(cr)
            layout.set_text("Cairo not available; chart disabled")
            desc = Pango.FontDescription("Cantarell 14")
            layout.set_font_description(desc)
            cr.set_source_rgba(1, 1, 1, 0.8)
            tw, th = layout.get_pixel_size()
            cr.move_to((w - tw) / 2, (h - th) / 2)
            PangoCairo.show_layout(cr, layout)
            return

        total, used, free = disk_usage_home()
        frac_used = (used / total) if total else 0.0

        # Colors (GNOME-ish)
        col_used = "#2ea3d6"
        col_free = "#51d08a"
        col_bg = "#3a3a3a"
        col_text = "#e6e6e6"

        def set_hex(hexcol: str, alpha=1.0):
            rgba = Gdk.RGBA()
            rgba.parse(hexcol)
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, alpha)

        pad = 24
        size = max(0, min(w, h) - pad * 2)
        r = size / 2
        cx, cy = pad + r, pad + r

        # Background ring
        set_hex(col_bg)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

        # Used slice
        start = -math.pi / 2
        used_ang = frac_used * 2 * math.pi
        set_hex(col_used)
        cr.move_to(cx, cy)
        cr.arc(cx, cy, r, start, start + used_ang)
        cr.close_path()
        cr.fill()

        # Free slice
        set_hex(col_free)
        cr.move_to(cx, cy)
        cr.arc(cx, cy, r, start + used_ang, start + 2 * math.pi)
        cr.close_path()
        cr.fill()

        # Center percentage text - moved to the rim to show used percentage
        pct = int(round(frac_used * 100))

        # Rim labels with clamping inside the frame
        def rim_label(angle_mid, text, color_hex):
            set_hex(color_hex)
            sx = cx + math.cos(angle_mid) * (r - 6)
            sy = cy + math.sin(angle_mid) * (r - 6)
            ex = cx + math.cos(angle_mid) * (r + 14)
            ey = cy + math.sin(angle_mid) * (r + 14)
            cr.set_line_width(2.0)
            cr.move_to(sx, sy)
            cr.line_to(ex, ey)
            cr.stroke()

            layout = PangoCairo.create_layout(cr)
            layout.set_text(text)
            layout.set_font_description(Pango.FontDescription("Cantarell 12"))
            tw, th = layout.get_pixel_size()

            tx = max(pad, min(w - pad - tw, ex + (8 if math.cos(angle_mid) >= 0 else -tw - 8)))
            ty = max(pad, min(h - pad - th, ey - th / 2))
            set_hex("#e8f3ff" if color_hex == col_used else "#defcee")
            cr.move_to(tx, ty)
            PangoCairo.show_layout(cr, layout)

        used_mid = start + used_ang / 2 if used_ang > 0 else start
        free_mid = start + used_ang + (2 * math.pi - used_ang) / 2

        rim_label(used_mid, f"{pct}% used — {human_size(used)}", col_used)
        rim_label(free_mid, f"Free — {human_size(free)}", col_free)

    # ─────────────── small UI helpers ───────────────

    def _toast(self, text: str):
        dlg = Adw.MessageDialog.new(self)
        dlg.set_heading("Spruce")
        dlg.set_body(text)
        dlg.add_response("ok", "OK")
        dlg.set_default_response("ok")
        dlg.present()
        return dlg

    def _info(self, text: str):
        self._toast(text)


# ─────────────────────────── app ─────────────────────────

class SpruceApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        Adw.init()

    def do_activate(self):
        win = self.props.active_window or SpruceWindow(application=self)
        win.present()


def main() -> int:
    app = SpruceApp()
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())

