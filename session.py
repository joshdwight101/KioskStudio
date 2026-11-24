"""Entry point embedded into each Kiosk Studio session export."""
import os
import shlex
import subprocess
from urllib.parse import urlsplit

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk


def validate_startup(settings):
    if not isinstance(settings, dict):
        raise ValueError('The autostart settings are invalid.')
    for path, options in settings.items():
        if (not isinstance(path, str) or not os.path.isabs(path) or '\0' in path or not isinstance(options, dict)
                or not isinstance(options.get('enabled'), bool)
                or not isinstance(options.get('arguments'), str) or not isinstance(options.get('urls'), str)):
            raise ValueError('An application has invalid autostart settings.')
    return settings


def startup_arguments(options):
    try:
        args = shlex.split(options['arguments'])
    except ValueError as error:
        raise ValueError('Check the quotes in the launch arguments.') from error
    if any('\0' in argument for argument in args):
        raise ValueError('Launch arguments cannot contain null bytes.')
    urls = [line.strip() for line in options['urls'].splitlines() if line.strip()]
    for url in urls:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {'http', 'https'} and parsed.hostname and not any(
                character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f'Use one complete HTTP or HTTPS URL per line: {url}')
    return args + urls


def quote_desktop_argument(argument):
    escaped = argument.replace('%', '%%')
    for character in ('\\', '"', '`', '$'):
        escaped = escaped.replace(character, '\\' + character)
    return f'"{escaped}"'


def launch_app(item, context, arguments=()):
    if item['kind'] == 'executable':
        subprocess.Popen([item['path'], *arguments], stdin=subprocess.DEVNULL, start_new_session=True)
        return
    if arguments:
        entry = GLib.KeyFile()
        entry.load_from_file(item['path'], GLib.KeyFileFlags.NONE)
        group = 'Desktop Entry'
        command = entry.get_string(group, 'Exec')
        entry.set_string(group, 'Exec', ' '.join([command, *map(quote_desktop_argument, arguments)]))
        entry.set_boolean(group, 'DBusActivatable', False)
        info = Gio.DesktopAppInfo.new_from_keyfile(entry)
    else:
        info = Gio.DesktopAppInfo.new_from_filename(item['path'])
    if info is None or not info.launch([], context):
        raise OSError('The application could not be started.')


def run_session(profile):
    if os.getuid() != profile['uid']:
        raise SystemExit(f"This session is assigned to {profile['user']}.")
    display_variables = [name for name in ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_SESSION_TYPE') if name in os.environ]
    if os.environ.get('DBUS_SESSION_BUS_ADDRESS') and display_variables:
        try:
            subprocess.run(['/usr/bin/dbus-update-activation-environment', *display_variables],
                           check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    app = Gtk.Application(application_id='io.github.joshuadwight.KioskSession')

    def activate(application):
        if application.get_active_window():
            application.get_active_window().present()
            return
        window = Gtk.ApplicationWindow(application=application, title='Kiosk applications')
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for edge in ('top', 'bottom', 'start', 'end'):
            getattr(content, f'set_margin_{edge}')(28)
        title = Gtk.Label(label='Available applications', xalign=0)
        title.add_css_class('title-1')
        content.append(title)
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5)
        status = Gtk.Label(wrap=True, xalign=0)

        def launch(_button, item):
            try:
                launch_app(item, window.get_display().get_app_launch_context())
                status.set_text('')
            except (OSError, GLib.Error) as error:
                status.set_text(f"{item['name']}: {error}")

        def autostart():
            failures = []
            for item in profile['apps']:
                options = profile.get('startup', {}).get(item['path'])
                if options and options['enabled']:
                    try:
                        arguments = startup_arguments(options)
                        launch_app(item, window.get_display().get_app_launch_context(), arguments)
                    except (OSError, ValueError, GLib.Error) as error:
                        failures.append(f"{item['name']}: {error}")
            status.set_text('\n'.join(failures))
            return GLib.SOURCE_REMOVE

        for item in profile['apps']:
            button = Gtk.Button(label=item['name'])
            button.connect('clicked', launch, item)
            grid.append(button)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=grid)
        content.append(scroll)
        content.append(status)
        window.set_child(content)
        window.fullscreen()
        window.present()
        GLib.idle_add(autostart)

    app.connect('activate', activate)
    return app.run([])
