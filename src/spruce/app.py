#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Spruce — GNOME Cleaner

from __future__ import annotations

import os
import re
import math
import shutil
from pathlib import Path
from typing import List, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango, PangoCairo  # type: ignore

try:
    import cairo  # type: ignore
except Exception:
    cairo = None

APP_ID = "io.github.shonubot.Spruce"
IS_FLATPAK = os.environ.get("FLATPAK_ID") == APP_ID
SPRUCE_DEBUG = os.environ.get("SPRUCE_DEBUG") == "1"


# ─────────────────────────── generic utils ───────────────────────────

def _find_ui() -> str:
    here = Path(__file__).resolve()
    for p in [
        here.parent.parent / "ui" / "window.ui",
        Path.cwd() / "ui" / "window.ui",
        Path("/app/share/io.github.shonubot.Spruce/ui/window.ui"),
        Path("/app/share/spruce/ui/window.ui"),
        Path("/app/ui/window.ui"),
    ]:
        if p.exists():
            return str(p)
    return "ui/window.ui"


def human_size(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024.0
    return f"{f:.1f}EiB"


def _gio_fs_usage(path: Path) -> Tuple[int, int, int] | None:
    try:
        info = Gio.File.new_for_path(str(path)).query_filesystem_info(
            "filesystem::size,filesystem::free", None
        )
        total = int(info.get_attribute_uint64("filesystem::size"))
        free = int(info.get_attribute_uint64("filesystem::free"))
        used = max(0, total - free)
        if total > 0 and free >= 0 and used >= 0:
            return total, used, free
    except Exception:
        pass
    return None


def disk_usage_home() -> Tuple[int, int, int]:
    # Good-enough for the chart; host mounts may not be visible
    p = Path.home()
    ans = _gio_fs_usage(p)
    if ans:
        return ans
    try:
        total, used, free = shutil.disk_usage(str(p))
        if total > 0:
            return int(total), int(used), int(free)
    except Exception:
        pass
    return 1, 0, 1


def xdg_cache() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))


def _host_exec(*argv: str) -> list[str]:
    """Build a command that runs on the host."""
    return ["flatpak-spawn", "--host", *argv] if IS_FLATPAK else list(argv)


def _spawn_capture(cmd: list[str]) -> tuple[int, str, str]:
    try:
        ok, out, err, st = GLib.spawn_sync(
            None, cmd, None,
            GLib.SpawnFlags.SEARCH_PATH
            | GLib.SpawnFlags.STDOUT_TO_PIPE
            | GLib.SpawnFlags.STDERR_TO_PIPE,
            None
        )
        return int(st) if isinstance(st, int) else 0, (out or b"").decode(), (err or b"").decode()
    except Exception as e:
        return 127, "", str(e)


def _spawn_host_shell(script: str) -> tuple[int, str, str]:
    """Run a small shell script on the HOST (no reliance on /run/host)."""
    return _spawn_capture(_host_exec("bash", "-lc", script))


def _debug_note(s: str):
    if not SPRUCE_DEBUG:
        return
    try:
        app = Gtk.Application.get_default()
        if app and app.props.active_window and hasattr(app.props.active_window, "pkg_list"):
            lbl = app.props.active_window.pkg_list
            lbl.set_text((lbl.get_text() + "\n" if lbl.get_text() else "") + s)
    except Exception:
        pass


# ────────── Flatpak (robust detection via host enumeration) ──────────

_APP_ID = re.compile(r"^[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)+$")
_RUNTIME_LINE = re.compile(r"^Runtime:\s*(.+?)\s*$", re.IGNORECASE)

def _list_apps(scope_flag: str) -> list[str]:
    # Avoid columns: they vary by version; parse first token from default table
    cmd = _host_exec("flatpak", "list", "--app", scope_flag)
    code, out, err = _spawn_capture(cmd)
    text = out if code == 0 else err
    apps: list[str] = []
    for ln in text.splitlines():
        tok = ln.strip().split()
        if tok and _APP_ID.match(tok[0]):
            apps.append(tok[0])
    _debug_note(f"{scope_flag} apps: {apps}")
    return apps


def _runtime_of_app(app_id: str, scope_flag: str) -> str | None:
    cmd = _host_exec("flatpak", "info", app_id, scope_flag)
    code, out, err = _spawn_capture(cmd)
    text = out if code == 0 else err
    for ln in text.splitlines():
        m = _RUNTIME_LINE.match(ln)
        if m:
            r = m.group(1).strip()
            return r if r.startswith("runtime/") else f"runtime/{r}"
    return None


def _used_runtime_refs(scope_flag: str) -> set[str]:
    used: set[str] = set()
    for app in _list_apps(scope_flag):
        r = _runtime_of_app(app, scope_flag)
        if r:
            used.add(r)
    _debug_note(f"{scope_flag} used runtimes: {sorted(used)}")
    return used


def _list_installed_runtime_refs(scope_flag: str) -> list[str]:
    """
    Ask the host to enumerate installed runtime refs by walking the well-known
    runtime roots. We avoid any `flatpak list` formatting quirks.
    """
    if scope_flag == "--user":
        script = r'''
roots=()
[ -n "$HOME" ] && roots+=("$HOME/.local/share/flatpak/runtime")
[ -n "$USER" ] && roots+=("/var/home/$USER/.local/share/flatpak/runtime")
seen=""
for root in "${roots[@]}"; do
  [ -d "$root" ] || continue
  # list rid/arch/branch dirs; ensure uniqueness across roots
  while IFS= read -r d; do
    rid=$(basename "$(dirname "$(dirname "$d")")")
    arch=$(basename "$(dirname "$d")")
    br=$(basename "$d")
    ref="runtime/$rid/$arch/$br"
    case ":$seen:" in
      *":$ref:"*) : ;;
      *) echo "$ref"; seen="$seen:$ref";;
    esac
  done < <(find "$root" -mindepth 3 -maxdepth 3 -type d | sort)
done
'''
    else:
        script = r'''
root="/var/lib/flatpak/runtime"
[ -d "$root" ] || exit 0
find "$root" -mindepth 3 -maxdepth 3 -type d \
| sort \
| awk -F/ 'BEGIN{OFS="/"} {rid=$(NF-2); arch=$(NF-1); br=$NF; print "runtime/"rid"/"arch"/"br}' \
| uniq
'''
    code, out, err = _spawn_host_shell(script)
    if SPRUCE_DEBUG and (err.strip() or code != 0):
        _debug_note(f"{scope_flag} host-enum err={err.strip()} code={code}")
    refs = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("runtime/")]
    _debug_note(f"{scope_flag} refs via host: {refs}")
    return refs


def _base_of(ref: str) -> tuple[str, str, str]:
    parts = ref.split("/", 4)
    return (parts[1], parts[2], parts[3]) if len(parts) >= 4 else (ref, "", "")


def _is_base_runtime(ref: str) -> bool:
    name, _, _ = _base_of(ref)
    if name.endswith(".Locale") or name.endswith(".Debug"):
        return False
    # treat GL.default, ffmpeg-full, openh264 etc. as extensions
    if any(name.endswith(suf) for suf in (".GL.default", ".ffmpeg-full", ".openh264", ".codecs", ".codecs-extra")):
        return False
    return True


def _platform_base_from_extension(ref: str) -> str:
    # e.g. runtime/org.freedesktop.Platform.ffmpeg-full/... -> org.freedesktop.Platform
    name, _, _ = _base_of(ref)
    parts = name.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 else name


def _unused_refs_for_scope(scope_flag: str) -> list[str]:
    installed = _list_installed_runtime_refs(scope_flag)
    apps = _list_apps(scope_flag)

    if not installed:
        return []

    if not apps:
        _debug_note(f"{scope_flag}: no apps; all installed refs considered candidates")
        return installed[:]

    used_bases = {_base_of(r)[0] for r in _used_runtime_refs(scope_flag)}

    unused: list[str] = []
    for ref in installed:
        name, _arch, _branch = _base_of(ref)
        if _is_base_runtime(ref):
            if name not in used_bases:
                unused.append(ref)
        else:
            plat = _platform_base_from_extension(ref)
            if plat not in used_bases:
                unused.append(ref)

    # de-dup preserve order
    seen, out = set(), []
    for r in unused:
        if r not in seen:
            seen.add(r)
            out.append(r)

    _debug_note(f"{scope_flag} unused refs: {out}")
    return out


def list_flatpak_unused() -> list[str]:
    refs: list[str] = []
    for scope in ("--user", "--system"):
        refs.extend(_unused_refs_for_scope(scope))
    # de-dup
    seen, out = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r); out.append(r)
    _debug_note(f"unused refs (merged): {out}")
    return out


def run_flatpak_autoremove_async(on_done) -> None:
    # Remove user first, then system; polkit may prompt for system scope
    user_cmd = _host_exec("flatpak", "uninstall", "--unused", "--user", "-y")
    sys_cmd  = _host_exec("flatpak", "uninstall", "--unused", "--system", "-y")

    def _spawn(cmd):
        try:
            pid, _, _ = GLib.spawn_async(cmd, flags=GLib.SpawnFlags.SEARCH_PATH)
            return pid
        except Exception:
            return None

    def after_user(_pid, _cond):
        pid2 = _spawn(sys_cmd)
        if pid2:
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid2, lambda *_: GLib.timeout_add_seconds(1, on_done))
        else:
            GLib.timeout_add_seconds(1, on_done)

    pid1 = _spawn(user_cmd)
    if pid1:
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid1, after_user)
    else:
        after_user(None, None)


# ─────────────────────────── cache utils ───────────────────────────

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


# ─────────────────────────── UI ───────────────────────────

@Gtk.Template(filename=_find_ui())
class SpruceWindow(Adw.ApplicationWindow):
    __gtype_name__ = "SpruceWindow"

    pie_chart: Gtk.DrawingArea = Gtk.Template.Child("pie_chart")
    clear_btn: Gtk.Button = Gtk.Template.Child("clear_btn")
    options_btn: Gtk.Button = Gtk.Template.Child("options_btn")
    pkg_list: Gtk.Label = Gtk.Template.Child("pkg_list")
    remove_btn: Gtk.Button = Gtk.Template.Child("remove_btn")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Chart
        self.pie_chart.set_hexpand(True)
        self.pie_chart.set_vexpand(True)
        self.pie_chart.set_content_width(360)
        self.pie_chart.set_content_height(260)
        self.pie_chart.set_draw_func(self._draw_chart, None)

        # Buttons
        self.clear_btn.connect("clicked", self._on_clear_clicked)
        self.options_btn.connect("clicked", self._on_options_clicked)
        self.remove_btn.connect("clicked", self._on_remove_clicked)
        self.timeout_source = None

        # Options state
        self._opts = {"thumbs": True, "webkit": True, "fontconf": True, "mesa": True, "sweep": True}
        self._current_toast = None

        # Initial UI
        self._refresh_autoremove_label()
        self.pie_chart.queue_draw()

    # ─────────────── unified cleanup card ───────────────

    def _refresh_autoremove_label(self):
        if self.timeout_source:
            GLib.source_remove(self.timeout_source)
            self.timeout_source = None

        pkgs = list_flatpak_unused()
        if pkgs:
            self.pkg_list.set_text(" ".join(pkgs))
            self.remove_btn.set_sensitive(True)
        else:
            self.pkg_list.set_text("No unused runtimes or extensions to remove.")
            self.remove_btn.set_sensitive(False)

        return GLib.SOURCE_REMOVE

    def _on_remove_clicked(self, _btn):
        run_flatpak_autoremove_async(self._refresh_autoremove_label)

    # ─────────────── clear temp / options ───────────────

    def _on_clear_clicked(self, _btn):
        if self._opts["sweep"]:
            self._current_toast = self._toast("Scanning cache directories...")
            GLib.Thread.new("cache_scanner", self._scan_cache_in_thread)
        else:
            if self._perform_instant_clears():
                self._toast("Selected caches cleared")
                self.pie_chart.queue_draw()

    def _perform_instant_clears(self):
        def rm_rf(p: Path) -> bool:
            try:
                if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
                elif p.exists(): p.unlink(missing_ok=True)
                return True
            except Exception:
                return False

        removed = False
        c = xdg_cache()
        if self._opts["thumbs"]:   removed |= rm_rf(c / "thumbnails")
        if self._opts["webkit"]:   removed |= rm_rf(c / "WebKitGTK") or rm_rf(c / "webkitgtk")
        if self._opts["fontconf"]: removed |= rm_rf(c / "fontconfig")
        if self._opts["mesa"]:     removed |= rm_rf(c / "mesa_shader_cache")
        return removed

    def _on_options_clicked(self, _btn):
        win = Adw.PreferencesWindow(transient_for=self, modal=True, title="Preferences")
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="What to clear when you press “Clear temp”")
        page.add(group)

        def add_switch(title, subtitle, key):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=self._opts[key])
            row.connect("notify::active", lambda r, *_: self._opts.__setitem__(key, r.get_active()))
            group.add(row)

        add_switch("Thumbnail cache", "~/.cache/thumbnails", "thumbs")
        add_switch("WebKitGTK caches", "~/.cache/WebKitGTK or ~/.cache/webkitgtk", "webkit")
        add_switch("Fontconfig cache", "~/.cache/fontconfig", "fontconf")
        add_switch("Mesa shader cache", "~/.cache/mesa_shader_cache", "mesa")

        g2 = Adw.PreferencesGroup(title="General")
        page.add(g2)
        row = Adw.SwitchRow(
            title="General cache sweep", subtitle="Pick large items in ~/.cache to remove", active=self._opts["sweep"]
        )
        row.connect("notify::active", lambda r, *_: self._opts.__setitem__("sweep", r.get_active()))
        g2.add(row)

        hb = Adw.HeaderBar()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(hb); box.append(page)
        win.set_content(box)
        win.present()

    # ─────────────── cache sweep (now includes host caches) ───────────────

    def _scan_cache_in_thread(self):
        """
        Build a list of (Path, size, can_delete, on_host) entries, then show dialog.
        - 'on_host' True  => path exists on host FS (remove with host rm -rf)
        - 'on_host' False => sandbox path (remove with shutil)
        """
        entries: list[tuple[Path, int, bool, bool]] = []

        # 1) Sandbox cache (always)
        sc = xdg_cache()
        if sc.is_dir():
            for child in sorted(sc.iterdir(), key=lambda p: p.name.lower()):
                try:
                    entries.append((child, dir_size(child), True, False))
                except Exception:
                    pass

        # 2) Host caches: $HOME/.cache and $HOME/.var/app/*/cache
        #    We query them via flatpak-spawn --host to be independent of /run/host.
        script = r'''
set -e
show_dir() { d="$1"; [ -d "$d" ] && echo "$d"; }
show_dir "$HOME/.cache"
if [ -d "$HOME/.var/app" ]; then
  find "$HOME/.var/app" -mindepth 2 -maxdepth 2 -type d -name cache -print
fi
'''
        _code, out, _err = _spawn_host_shell(script)
        host_paths = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("/")]
        # We can't stat host sizes directly; approximate by asking host for du -sb
        if host_paths:
            du_script = "set -e; " + " ; ".join(
                [f'if [ -e "{p}" ]; then du -sb "{p}" | awk "{{print $1, \\"{p}\\"}}"; fi' for p in host_paths]
            )
            _c2, out2, _e2 = _spawn_host_shell(du_script)
            sizes: dict[str, int] = {}
            for ln in out2.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    sz, path = ln.split(" ", 1)
                    sizes[path] = int(sz)
                except Exception:
                    continue
            for p in host_paths:
                entries.append((Path(p), sizes.get(p, 0), True, True))

        GLib.idle_add(self._show_sweep_dialog, entries)
        return None

    def _show_sweep_dialog(self, entries: list[tuple[Path, int, bool, bool]]):
        if self._current_toast:
            self._current_toast.close(); self._current_toast = None

        dlg = Adw.Dialog.new()
        dlg.set_title("Cache sweep")
        dlg.present(self)
        dlg.set_content_width(720)
        dlg.set_content_height(520)

        header = Adw.HeaderBar()
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        v.set_margin_top(12); v.set_margin_bottom(12); v.set_margin_start(12); v.set_margin_end(12)

        title = Gtk.Label(label="Select the cache files to remove:", xalign=0)
        title.add_css_class("title-4"); v.append(title)

        sc = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        listbox = Gtk.ListBox(); listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        sc.set_child(listbox); v.append(sc)

        toggles: List[Gtk.Switch] = []
        paths: List[Path] = []
        deletable: List[bool] = []
        on_host_flags: List[bool] = []

        for p, sz, can_delete, on_host in sorted(entries, key=lambda t: t[1], reverse=True):
            loc = "host" if on_host else "sandbox"
            row = Adw.ActionRow(title=p.name, subtitle=f"{p} ({loc}) — {human_size(sz)}")
            sw = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=can_delete)
            row.add_suffix(sw)
            listbox.append(row)
            toggles.append(sw); paths.append(p); deletable.append(can_delete); on_host_flags.append(on_host)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        sel_all = Gtk.CheckButton(label="Select all")
        rm_btn = Gtk.Button(label="Remove selected", sensitive=False)
        actions.append(sel_all); actions.append(rm_btn); v.append(actions)

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
            host_targets: list[str] = []
            for sw, p, can_delete, on_host in zip(toggles, paths, deletable, on_host_flags):
                if not can_delete or not sw.get_active():
                    continue
                if on_host:
                    host_targets.append(str(p))
                else:
                    try:
                        if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
                        else: p.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass
            if host_targets:
                # rm -rf on the host for each target (quote safely)
                script = " ; ".join([f'rm -rf -- "{t}"' for t in host_targets])
                _spawn_host_shell(script)
                removed += len(host_targets)

            if removed:
                self._toast(f"Removed {removed} item(s)")
                self.pie_chart.queue_draw()
            dlg.close()

        rm_btn.connect("clicked", do_rm)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.append(header); body.append(v)
        dlg.set_child(body)

    # ─────────────── pie chart ───────────────

    def _draw_chart(self, _area, cr, w: int, h: int, _data):
        if cairo is None:
            layout = PangoCairo.create_layout(cr)
            layout.set_text("Cairo not available; chart disabled")
            layout.set_font_description(Pango.FontDescription("Cantarell 14"))
            cr.set_source_rgba(1, 1, 1, 0.8)
            tw, th = layout.get_pixel_size()
            cr.move_to((w - tw)/2, (h - th)/2); PangoCairo.show_layout(cr, layout); return

        total, used, free = disk_usage_home(); frac_used = (used / total) if total else 0.0
        col_used, col_free, col_bg, col_text = "#2ea3d6", "#51d08a", "#3a3a3a", "#e6e6e6"

        def set_hex(hexcol: str, a=1.0):
            rgba = Gdk.RGBA(); rgba.parse(hexcol)
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, a)

        pad = 24; size = max(0, min(w, h) - pad*2); r = size/2; cx, cy = pad + r, pad + r
        set_hex(col_bg); cr.arc(cx, cy, r, 0, 2*math.pi); cr.fill()

        start = -math.pi/2; used_ang = frac_used * 2*math.pi
        set_hex(col_used); cr.move_to(cx, cy); cr.arc(cx, cy, r, start, start+used_ang); cr.close_path(); cr.fill()
        set_hex(col_free); cr.move_to(cx, cy); cr.arc(cx, cy, r, start+used_ang, start+2*math.pi); cr.close_path(); cr.fill()

        pct = int(round(frac_used * 100))
        layout = PangoCairo.create_layout(cr); layout.set_text(f"{pct}%")
        layout.set_font_description(Pango.FontDescription("Cantarell Bold 40"))
        tw, th = layout.get_pixel_size(); set_hex(col_text, 0.95)
        cr.move_to(cx - tw/2, cy - th/2); PangoCairo.show_layout(cr, layout)

        def rim_label(a_mid, txt, col):
            set_hex(col); sx = cx + math.cos(a_mid)*(r-6); sy = cy + math.sin(a_mid)*(r-6)
            ex = cx + math.cos(a_mid)*(r+14); ey = cy + math.sin(a_mid)*(r+14)
            cr.set_line_width(2.0); cr.move_to(sx, sy); cr.line_to(ex, ey); cr.stroke()
            layout = PangoCairo.create_layout(cr); layout.set_text(txt)
            layout.set_font_description(Pango.FontDescription("Cantarell 12")); tw, th = layout.get_pixel_size()
            tx = max(pad, min(w - pad - tw, ex + (8 if math.cos(a_mid) >= 0 else -tw - 8)))
            ty = max(pad, min(h - pad - th, ey - th/2))
            set_hex("#e8f3ff" if col == col_used else "#defcee")
            cr.move_to(tx, ty); PangoCairo.show_layout(cr, layout)

        used_mid = start + used_ang/2 if used_ang > 0 else start
        free_mid = start + used_ang + (2*math.pi - used_ang)/2
        rim_label(used_mid, f"Used — {human_size(used)}", col_used)
        rim_label(free_mid, f"Free — {human_size(free)}", col_free)

    # ─────────────── small UI helper ───────────────

    def _toast(self, text: str):
        dlg = Adw.AlertDialog.new("Spruce", text)
        dlg.add_response("ok", "OK"); dlg.set_default_response("ok"); dlg.present(self)
        return dlg


# ─────────────────────────── app ───────────────────────────

class SpruceApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        Adw.init()
    def do_activate(self):
        (self.props.active_window or SpruceWindow(application=self)).present()


def main() -> int:
    return SpruceApp().run([])


if __name__ == "__main__":
    raise SystemExit(main())
