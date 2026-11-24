#!/usr/bin/python3
"""Configure an approved application launcher for a dedicated kiosk user."""
import grp
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import tempfile
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk
from session import quote_desktop_argument, startup_arguments, validate_startup

APP_ID = 'io.github.joshuadwight.KioskStudio'
AUTHOR = 'Joshua Dwight'
VERSION = '1.1.0'
DATA = Path(GLib.get_user_data_dir())
SETTINGS = Path(GLib.get_user_config_dir()) / 'kiosk-studio' / 'profile.json'
SOURCE = Path(__file__).resolve().parent
PROFILE_PREFIX = '# Kiosk Studio profile: '


def detect_environment():
    try:
        distro = platform.freedesktop_os_release().get('PRETTY_NAME', 'Linux')
    except OSError:
        distro = 'Linux'
    desktop = os.getenv('XDG_CURRENT_DESKTOP') or os.getenv('XDG_SESSION_DESKTOP') or 'Unknown desktop'
    session = os.getenv('XDG_SESSION_TYPE', 'unknown session')
    manager_unit = Path('/etc/systemd/system/display-manager.service')
    manager = manager_unit.resolve().stem if manager_unit.is_symlink() else 'Unknown login manager'
    gnome_kiosk = Path('/usr/share/wayland-sessions/gnome-kiosk-script-wayland.desktop').is_file()
    runtimes = [name for name, found in [('Cage', shutil.which('cage')), ('GNOME Kiosk', gnome_kiosk)] if found]
    return f"{distro} · {desktop} · {session} · {manager} · {', '.join(runtimes) or 'No kiosk runtime detected'}"


def local_users():
    uid_min = 1000
    for line in Path('/etc/login.defs').read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == 'UID_MIN':
            uid_min = int(fields[1])
            break
    return sorted(
        (user for user in pwd.getpwall()
         if uid_min <= user.pw_uid < 65534 and Path(user.pw_shell).name not in {'false', 'nologin'}),
        key=lambda user: user.pw_name,
    )


def discover(extra_paths):
    apps = {}
    for info in Gio.AppInfo.get_all():
        if (isinstance(info, Gio.DesktopAppInfo) and info.should_show()
                and not info.get_boolean('Terminal') and info.get_filename()
                and info.get_id() != f'{APP_ID}.desktop'):
            path = str(Path(info.get_filename()).absolute())
            apps[path] = {'name': info.get_display_name(), 'path': path, 'kind': 'desktop'}
    errors = []
    for directory in extra_paths:
        try:
            for entry in Path(directory).iterdir():
                if not entry.is_file() or str(entry) in apps:
                    continue
                if entry.suffix == '.desktop':
                    info = Gio.DesktopAppInfo.new_from_filename(str(entry))
                    if info and not info.get_boolean('Terminal'):
                        apps[str(entry)] = {'name': info.get_display_name(), 'path': str(entry), 'kind': 'desktop'}
                elif os.access(entry, os.X_OK):
                    apps[str(entry)] = {'name': entry.name, 'path': str(entry), 'kind': 'executable'}
        except OSError as error:
            errors.append(str(error))
    return sorted(apps.values(), key=lambda item: (item['name'].casefold(), item['path'])), errors


def write_file(path, data):
    Gio.File.new_for_path(str(path)).replace_contents(data, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError('Choose a Kiosk Studio settings backup or session export.')
    user = settings.get('user', '')
    autologin = settings.get('autologin', False)
    if not isinstance(user, str) or not isinstance(autologin, bool):
        raise ValueError('The account or automatic-start setting is invalid.')
    result = {'user': user, 'autologin': autologin}
    for key in ('paths', 'selected'):
        values = settings.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or '\0' in value or not Path(value).is_absolute()
            or (key == 'paths' and ':' in value) for value in values
        ):
            raise ValueError('The backup contains an invalid app or folder path.')
        result[key] = list(dict.fromkeys(values))
    result['startup'] = validate_startup(settings.get('startup', {}))
    return result


def read_settings_backup(path):
    with path.open(encoding='utf-8-sig') as stream:
        if stream.readline().startswith('#!'):
            metadata = stream.readline()
            if not metadata.startswith(PROFILE_PREFIX):
                raise ValueError('This script does not contain Kiosk Studio session settings.')
            profile = json.loads(metadata[len(PROFILE_PREFIX):])
            if not isinstance(profile, dict) or not isinstance(profile.get('apps'), list):
                raise ValueError('The session export does not contain an app list.')
            selected = [item.get('path') if isinstance(item, dict) else None for item in profile['apps']]
            settings = validate_settings({**profile, 'selected': selected})
            if 'paths' not in profile:
                # Older session exports only recorded the selected app paths.
                installed, _ = discover(())
                known = {app['path'] for app in installed}
                settings['paths'] = list(dict.fromkeys(
                    str(Path(app).parent) for app in settings['selected'] if app not in known
                ))
            return validate_settings(settings)
        stream.seek(0)
        backup = json.load(stream)
    if not isinstance(backup, dict) or backup.get('format') != 'kiosk-studio-settings' or backup.get('version') != 1:
        raise ValueError('Choose a Kiosk Studio settings backup or session export.')
    return validate_settings(backup.get('settings'))


class KioskStudio(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.connect('activate', self.activate)
        self.chooser = None
        self.admin_process = None
        try:
            self.settings = validate_settings(json.loads(SETTINGS.read_text()))
        except (OSError, ValueError):
            self.settings = {}
        self.startup = self.settings.get('startup', {}).copy()

    def activate(self, *_):
        if self.get_active_window():
            self.get_active_window().present()
            return
        self.win = Adw.ApplicationWindow(application=self, title='Kiosk Studio', default_width=900, default_height=730)
        self.win.set_icon_name(APP_ID)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        branding = Gtk.Box(spacing=8)
        logo = Gtk.Image.new_from_icon_name(APP_ID)
        logo.set_pixel_size(26)
        branding.append(logo)
        branding.append(Adw.WindowTitle(title='Kiosk Studio', subtitle=f'{AUTHOR} · v{VERSION}'))
        header.set_title_widget(branding)

        for label, items in [
            ('File', [('Create Kiosk Session…', 'export', self.export),
                      ('Back Up Settings…', 'export-settings', self.export_settings),
                      ('Restore Settings…', 'import-settings', self.import_settings),
                      ('Install Desktop Shortcut', 'shortcut', self.shortcut),
                      ('Install in Application Menu', 'install-app-menu', self.install_app_menu)]),
            ('Help', [('User Guide', 'user-guide', self.show_user_guide), ('About', 'about', self.about)]),
        ]:
            menu = Gio.Menu()
            for caption, name, callback in items:
                action = Gio.SimpleAction.new(name, None)
                action.connect('activate', callback)
                self.add_action(action)
                menu.append(caption, f'app.{name}')
            header.pack_start(Gtk.MenuButton(label=label, menu_model=menu))
        export = Gtk.Button(label='Create Kiosk Session…', css_classes=['suggested-action'])
        export.set_tooltip_text('Create a session file for testing or deployment')
        export.connect('clicked', self.export)
        header.pack_end(export)
        toolbar.add_top_bar(header)
        self.win.set_content(toolbar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ('top', 'bottom', 'start', 'end'):
            getattr(content, f'set_margin_{edge}')(20)
        toolbar.set_content(content)
        content.append(Gtk.Label(label=detect_environment(), xalign=0, wrap=True, css_classes=['dim-label']))
        self.users = local_users()
        self.user = Gtk.DropDown.new_from_strings(['Choose an account'] + [u.pw_name for u in self.users])
        self.user.set_selected(next((i for i, u in enumerate(self.users, 1) if u.pw_name == self.settings.get('user')), 0))
        reload_accounts = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Refresh local accounts')
        reload_accounts.connect('clicked', lambda *_: self.refresh_accounts())
        self.auto = Gtk.CheckButton(label='Start kiosk automatically at boot', active=self.settings.get('autologin', False))
        account_row = Gtk.Box(spacing=12)
        account_row.append(Gtk.Label(label='Kiosk account'))
        account_row.append(self.user)
        account_row.append(reload_accounts)
        account_row.append(self.auto)
        content.append(account_row)
        content.append(Gtk.Label(label='Use a dedicated standard account for the kiosk.', xalign=0, css_classes=['dim-label']))

        self.tabs = Gtk.Stack(vexpand=True)
        switcher = Gtk.StackSwitcher(stack=self.tabs, halign=Gtk.Align.START)
        content.append(switcher)
        content.append(self.tabs)
        approved = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.tabs.add_titled(approved, 'approved', 'Approved Apps')

        paths = Gtk.Box(spacing=8)
        paths.append(Gtk.Label(label='Additional app folders'))
        self.paths = Gtk.Entry(placeholder_text='Separate folder paths with colons', hexpand=True)
        self.paths.set_text(':'.join(self.settings.get('paths', [])))
        self.paths.connect('activate', self.refresh)
        paths.append(self.paths)
        refresh = Gtk.Button(label='Scan Apps')
        refresh.connect('clicked', self.refresh)
        paths.append(refresh)
        approved.append(paths)

        self.search = Gtk.SearchEntry(placeholder_text='Filter installed apps')
        self.search.connect('search-changed', lambda *_: self.apps.invalidate_filter())
        approved.append(self.search)
        self.apps = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        self.apps.set_filter_func(lambda row: self.search.get_text().casefold() in
                                  f"{row.item['name']} {row.item['path']}".casefold())
        approved.append(Gtk.ScrolledWindow(vexpand=True, child=self.apps))
        self.status = Gtk.Label(xalign=0, wrap=True)
        approved.append(self.status)

        self.startup_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        startup_page = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.startup_rows)
        self.tabs.add_titled(startup_page, 'startup', 'Autostart')
        self.tabs.connect('notify::visible-child-name', self.refresh_autostart)
        self.tabs.add_titled(self.accounts_page(), 'accounts', 'Accounts')
        self.tabs.add_titled(self.deployment_page(), 'deployment', 'Deployment')
        self.rows = []
        self.unavailable_apps = set(self.settings.get('selected', []))
        self.refresh()
        self.win.present()

    def accounts_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        create = Adw.PreferencesGroup(
            title='Create a standard account',
            description='Creates a home folder and selects the new account for kiosk setup. Password sign-in starts locked.',
        )
        self.new_user = Adw.EntryRow(title='Username')
        self.new_user.connect('entry-activated', self.create_user)
        create.add(self.new_user)
        self.initial_group = Adw.ComboRow(title='Additional group')
        create.add(self.initial_group)
        button = Gtk.Button(label='Create Account', halign=Gtk.Align.START, css_classes=['suggested-action'])
        button.connect('clicked', self.create_user)
        page.append(create)
        page.append(button)

        membership = Adw.PreferencesGroup(title='Add a user to a group')
        self.group_user = Adw.ComboRow(title='Account')
        self.assign_group = Adw.ComboRow(title='Group')
        membership.add(self.group_user)
        membership.add(self.assign_group)
        assign = Gtk.Button(label='Add to Group', halign=Gtk.Align.START)
        assign.connect('clicked', self.add_to_group)
        page.append(membership)
        page.append(assign)

        groups = Adw.PreferencesGroup(title='Create a group')
        self.new_group = Adw.EntryRow(title='Group name')
        self.new_group.connect('entry-activated', self.create_group)
        groups.add(self.new_group)
        add_group = Gtk.Button(label='Create Group', halign=Gtk.Align.START)
        add_group.connect('clicked', self.create_group)
        page.append(groups)
        page.append(add_group)
        self.refresh_accounts()
        return Gtk.ScrolledWindow(child=page, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def refresh_accounts(self, preferred_user=None, preferred_group=None):
        model = self.user.get_model()
        current = preferred_user or self.dropdown_text(self.user)
        self.users = local_users()
        names = ['Choose an account', *(user.pw_name for user in self.users)]
        model.splice(0, model.get_n_items(), names)
        self.user.set_selected(names.index(current) if current in names else 0)
        self.group_user.set_model(Gtk.StringList.new(names))
        self.group_user.set_selected(self.user.get_selected())
        groups = ['Choose a group', *sorted({group.gr_name for group in grp.getgrall()})]
        for dropdown in (self.initial_group, self.assign_group):
            selected = preferred_group or self.dropdown_text(dropdown)
            dropdown.set_model(Gtk.StringList.new(groups))
            dropdown.set_selected(groups.index(selected) if selected in groups else 0)

    @staticmethod
    def dropdown_text(dropdown):
        item = dropdown.get_selected_item()
        return item.get_string() if item and dropdown.get_selected() > 0 else None

    def create_user(self, *_):
        name = self.new_user.get_text().strip()
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name):
            self.message('Check the username', 'Start with a lowercase letter or underscore. Use up to 32 lowercase letters, digits, underscores, or hyphens.')
            return
        arguments = ['/usr/sbin/useradd', '--create-home', '--user-group', '--shell', '/bin/bash']
        group = self.dropdown_text(self.initial_group)
        if group:
            arguments.extend(['--groups', group])

        def created():
            self.new_user.set_text('')
            self.refresh_accounts(preferred_user=name)

        self.run_admin([*arguments, '--', name], 'Account created', created)

    def create_group(self, *_):
        name = self.new_group.get_text().strip()
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name):
            self.message('Check the group name', 'Start with a lowercase letter or underscore. Use up to 32 lowercase letters, digits, underscores, or hyphens.')
            return

        def created():
            self.new_group.set_text('')
            self.refresh_accounts(preferred_group=name)

        self.run_admin(['/usr/sbin/groupadd', '--', name], 'Group created', created)

    def add_to_group(self, *_):
        user = self.dropdown_text(self.group_user)
        group = self.dropdown_text(self.assign_group)
        if not user or not group:
            self.message('Choose an account and group', 'Both selections are required.')
            return
        self.run_admin(['/usr/sbin/usermod', '--append', '--groups', group, '--', user],
                       'Group membership updated', self.refresh_accounts)

    def deployment_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.runtime_status = Gtk.Label(xalign=0, wrap=True)
        self.refresh_runtime_status()
        page.append(self.runtime_status)
        install = Gtk.Button(label='Install Cage Runtime', halign=Gtk.Align.START)
        install.connect('clicked', lambda *_: self.confirm_admin(
            'Install Cage runtime?', 'Ubuntu or Mint will install the packages needed for automatic kiosk startup.',
            lambda: self.run_admin(['/usr/bin/apt-get', 'install', '--yes', 'cage', 'dbus-daemon', 'kbd'],
                                   'Cage runtime installed', self.refresh_runtime_status),
        ))
        page.append(install)
        page.append(Gtk.Separator())
        deploy = Gtk.Button(label='Deploy Current Setup…', halign=Gtk.Align.START, css_classes=['suggested-action'])
        deploy.connect('clicked', self.deploy_current)
        page.append(deploy)
        page.append(Gtk.Label(label='Uses the selected account, approved apps, and Autostart choices on the next reboot.',
                              xalign=0, wrap=True))
        existing = Gtk.Button(label='Deploy Session File…', halign=Gtk.Align.START)
        existing.connect('clicked', lambda *_: self.choose_settings_file(
            'Deploy Session File', Gtk.FileChooserAction.OPEN, 'Review', self.deploy_file,
        ))
        page.append(existing)
        remove = Gtk.Button(label='Restore Normal Boot…', halign=Gtk.Align.START)
        remove.connect('clicked', lambda *_: self.confirm_admin(
            'Restore normal boot?', 'The computer will return to its previous boot mode the next time it restarts.',
            lambda: self.run_admin(self.deploy_command('--remove'), 'Normal boot restored'),
        ))
        page.append(remove)
        return page

    def refresh_runtime_status(self):
        ready = all(Path(path).is_file() for path in ('/usr/bin/cage', '/usr/bin/chvt', '/usr/bin/dbus-run-session'))
        self.runtime_status.set_text('Cage runtime is installed.' if ready else 'Install the Cage runtime before deployment.')

    @staticmethod
    def deploy_command(*arguments):
        return ['/usr/bin/python3', str(SOURCE / 'deploy.py'), *arguments]

    def deploy_file(self, path):
        self.confirm_admin('Deploy this kiosk session?',
                           f'{path}\n\nThe kiosk will start automatically on the next reboot.',
                           lambda: self.run_admin(self.deploy_command('--apply', str(path)), 'Kiosk deployment saved'))

    def deploy_current(self, *_):
        profile = self.session_profile()
        if profile is None:
            return
        if not profile['autologin']:
            self.message('Enable automatic startup', 'Turn on Start kiosk automatically at boot for the chosen account.')
            return
        self.confirm_admin('Deploy the current kiosk setup?',
                           f"The kiosk will start as {profile['user']} on the next reboot. "
                           'Keep a separate administrator account for recovery.',
                           lambda: self.apply_current_profile(profile))

    def apply_current_profile(self, profile):
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', prefix='kiosk-studio-', suffix='.py',
                                             encoding='utf-8', delete=False) as stream:
                path = Path(stream.name)
                stream.write(self.session_contents(profile))
            self.save_editor_settings(self.editor_settings())
            if self.run_admin(self.deploy_command('--apply', str(path)), 'Kiosk deployment saved',
                              cleanup=lambda: path.unlink(missing_ok=True)):
                return
        except (OSError, GLib.Error) as error:
            self.message('Could not prepare deployment', str(error))
        if path is not None:
            path.unlink(missing_ok=True)

    def confirm_admin(self, title, body, callback):
        dialog = Adw.MessageDialog(transient_for=self.win, heading=title, body=body)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('continue', 'Continue')
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _dialog, response: callback() if response == 'continue' else None)
        dialog.present()

    def run_admin(self, command, success, callback=None, cleanup=None):
        if self.admin_process is not None:
            self.message('Administrator action in progress', 'Wait for the current action to finish.')
            return False
        try:
            self.admin_process = Gio.Subprocess.new(
                ['/usr/bin/pkexec', *command],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error as error:
            self.message('Could not start administrator action', str(error))
            return False

        def finished(process, result):
            self.admin_process = None
            if cleanup:
                cleanup()
            try:
                _, output, errors = process.communicate_utf8_finish(result)
                if process.get_successful():
                    if callback:
                        callback()
                    self.message(success, output.strip() or 'Changes saved.')
                else:
                    self.message('Administrator action did not complete', (errors or output).strip() or 'No changes confirmed.')
            except GLib.Error as error:
                self.message('Administrator action did not complete', str(error))

        self.admin_process.communicate_utf8_async(None, None, finished)
        return True

    def selected_apps(self):
        return [item for item, check in self.rows if check.get_active()]

    def extra_paths(self):
        return list(dict.fromkeys(str(Path(s.strip()).expanduser().absolute())
                                  for s in self.paths.get_text().split(':') if s.strip()))

    def refresh(self, *_):
        selected = {item['path'] for item in self.selected_apps()} | self.unavailable_apps
        for row in self.rows:
            self.apps.remove(row[1].get_parent())
        self.rows = []
        apps, errors = discover(self.extra_paths())
        self.unavailable_apps = selected - {item['path'] for item in apps}
        for item in apps:
            row = Gtk.ListBoxRow()
            row.item = item
            check = Gtk.CheckButton(label=item['name'], active=item['path'] in selected, tooltip_text=item['path'])
            for edge in ('top', 'bottom', 'start', 'end'):
                getattr(check, f'set_margin_{edge}')(8)
            row.set_child(check)
            self.apps.append(row)
            self.rows.append((item, check))
        self.status.set_text(f'{len(apps)} apps detected. Select the apps to show on the kiosk.'
                             + (f' {len(self.unavailable_apps)} saved apps are unavailable.' if self.unavailable_apps else '')
                             + (' ' + ' '.join(errors) if errors else ''))
        self.refresh_autostart()

    def refresh_autostart(self, *_):
        if self.tabs.get_visible_child_name() != 'startup':
            return
        while child := self.startup_rows.get_first_child():
            self.startup_rows.remove(child)
        apps = self.selected_apps()
        intro = 'Choose apps in Approved Apps to configure their startup here.'
        if apps:
            intro = 'Choose which approved apps open automatically when the kiosk session starts.'
        self.startup_rows.append(Gtk.Label(label=intro, xalign=0, wrap=True))
        for item in apps:
            self.startup_rows.append(self.autostart_row(item))

    def autostart_row(self, item):
        path = item['path']
        options = self.startup.get(path, {'enabled': False, 'arguments': '', 'urls': ''})
        group = Adw.PreferencesGroup(title=item['name'])
        toggle = Adw.ActionRow(title='Launch at startup')
        enabled = Gtk.Switch(active=options['enabled'], valign=Gtk.Align.CENTER)
        toggle.add_suffix(enabled)
        toggle.set_activatable_widget(enabled)
        group.add(toggle)

        arguments = Adw.EntryRow(title='Launch arguments', text=options['arguments'])
        arguments.set_tooltip_text('Example: --kiosk --profile "Public Browser"')
        group.add(arguments)

        urls = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                            left_margin=8, right_margin=8)
        buffer = urls.get_buffer()
        buffer.set_text(options['urls'])
        scroll = Gtk.ScrolledWindow(min_content_height=65, propagate_natural_height=True, child=urls)
        url_row = Adw.ExpanderRow(title='Browser URLs', subtitle='One HTTP or HTTPS URL per line')
        url_row.set_expanded(bool(options['urls']))
        url_row.add_row(scroll)
        group.add(url_row)

        def update(*_):
            self.startup[path] = {
                'enabled': enabled.get_active(),
                'arguments': arguments.get_text(),
                'urls': buffer.get_text(*buffer.get_bounds(), True),
            }

        enabled.connect('notify::active', update)
        arguments.connect('changed', update)
        buffer.connect('changed', update)
        return group

    def editor_settings(self):
        index = self.user.get_selected()
        selected = [item['path'] for item in self.selected_apps()] + sorted(self.unavailable_apps)
        return {
            'user': self.users[index - 1].pw_name if index else '',
            'autologin': self.auto.get_active(),
            'paths': self.extra_paths(),
            'selected': selected,
            'startup': {path: self.startup[path] for path in selected if path in self.startup},
        }

    def save_editor_settings(self, settings):
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        write_file(SETTINGS, json.dumps(settings).encode())
        self.settings = settings

    def export_settings(self, *_):
        self.choose_settings_file('Back Up Settings', Gtk.FileChooserAction.SAVE, 'Save Backup', self.export_settings_to)

    def import_settings(self, *_):
        self.choose_settings_file('Restore Settings', Gtk.FileChooserAction.OPEN, 'Restore', self.import_settings_from)

    def choose_settings_file(self, title, action, button, callback):
        chooser = Gtk.FileChooserNative.new(title, self.win, action, button, 'Cancel')
        chooser.set_modal(True)
        if action == Gtk.FileChooserAction.SAVE:
            chooser.set_current_name('kiosk-studio-settings.json')
        chooser.connect('response', self.settings_file_chosen, callback)
        self.chooser = chooser
        chooser.show()

    def settings_file_chosen(self, chooser, response, callback):
        file = chooser.get_file() if response == Gtk.ResponseType.ACCEPT else None
        chooser.destroy()
        self.chooser = None
        if file is None:
            return
        try:
            path = file.get_path()
            if path is None:
                raise OSError('Choose a local file.')
            callback(Path(path))
        except (OSError, ValueError, GLib.Error) as error:
            self.message('Could not use settings file', str(error))

    def export_settings_to(self, path):
        backup = {'format': 'kiosk-studio-settings', 'version': 1, 'settings': self.editor_settings()}
        write_file(path, (json.dumps(backup, indent=2) + '\n').encode())
        self.message('Settings backup saved', str(path))

    def import_settings_from(self, path):
        settings = read_settings_backup(path)
        index = next((i for i, user in enumerate(self.users, 1) if user.pw_name == settings['user']), 0)
        self.save_editor_settings(settings)
        self.user.set_selected(index)
        self.auto.set_active(settings['autologin'])
        self.startup = settings['startup'].copy()
        self.paths.set_text(':'.join(settings['paths']))
        for _, check in self.rows:
            check.set_active(False)
        self.unavailable_apps = set(settings['selected'])
        self.search.set_text('')
        self.refresh()
        message = 'The backup settings are loaded in the kiosk editor.'
        if settings['user'] and not index:
            message += f" The account {settings['user']} is unavailable. Choose a local account before exporting a session."
        if self.unavailable_apps:
            message += f' Apps missing from this computer: {len(self.unavailable_apps)}.'
        self.message('Settings restored', message)

    def session_profile(self):
        selected = self.user.get_selected()
        apps = self.selected_apps()
        if not selected or not apps:
            self.message('Choose an account and at least one app', 'The kiosk displays only the apps selected here.')
            return
        user = self.users[selected - 1]
        groups = {grp.getgrgid(gid).gr_name for gid in os.getgrouplist(user.pw_name, user.pw_gid)}
        if groups & {'sudo', 'admin', 'wheel', 'root', 'docker', 'lxd', 'incus', 'disk', 'libvirt'}:
            self.message('Choose a standard account', f'{user.pw_name} belongs to a group with system-level access.')
            return
        startup = {item['path']: self.startup[item['path']] for item in apps if item['path'] in self.startup}
        for item in apps:
            options = startup.get(item['path'])
            if options and options['enabled']:
                try:
                    startup_arguments(options)
                except ValueError as error:
                    self.tabs.set_visible_child_name('startup')
                    self.message(f"Check autostart for {item['name']}", str(error))
                    return
        return {'uid': user.pw_uid, 'user': user.pw_name, 'autologin': self.auto.get_active(),
                'apps': apps, 'paths': self.extra_paths(), 'startup': startup}

    @staticmethod
    def session_contents(profile):
        metadata = json.dumps(profile, ensure_ascii=True)
        session = (SOURCE / 'session.py').read_text()
        return (f'#!/usr/bin/python3\n{PROFILE_PREFIX}{metadata}\n{session}\n'
                f"\nif __name__ == '__main__':\n    raise SystemExit(run_session({profile!r}))\n")

    def export(self, *_):
        profile = self.session_profile()
        if profile is None:
            return
        chooser = Gtk.FileChooserNative.new('Create Kiosk Session', self.win, Gtk.FileChooserAction.SAVE, 'Create', 'Cancel')
        chooser.set_current_name(f"kiosk-{profile['user']}.py")
        chooser.set_modal(True)
        chooser.connect('response', self.export_chosen, profile)
        self.chooser = chooser
        chooser.show()

    def export_chosen(self, chooser, response, profile):
        file = chooser.get_file() if response == Gtk.ResponseType.ACCEPT else None
        chooser.destroy()
        self.chooser = None
        if file is None:
            return
        try:
            path = file.get_path()
            if path is None:
                raise OSError('Choose a local folder.')
            write_file(Path(path), self.session_contents(profile).encode())
            self.save_editor_settings(self.editor_settings())
        except (OSError, GLib.Error) as error:
            self.message('Could not export session', str(error))
            return
        if profile['autologin']:
            deploy = shlex.quote(str(SOURCE / 'deploy.py'))
            self.message('Kiosk session exported', f'{path}\n\nApply for next boot from an administrator terminal:\n'
                         f'sudo /usr/bin/python3 {deploy} --apply {shlex.quote(path)}')
        else:
            self.message('Kiosk session exported', f'{path}\nSee Help → User Guide to start it manually.')

    def install_app_menu(self, *_):
        app_id = self.get_application_id()
        script = Path(__file__).resolve()
        icon_file = script.with_name(f'{app_id}.svg')
        folder = Path(GLib.get_user_data_dir()) / 'applications'
        target = folder / f'{APP_ID}.desktop'

        entry = GLib.KeyFile()
        properties = {
            'Type': 'Application',
            'Name': self.win.get_title(),
            'Comment': 'Configure approved applications for a kiosk session',
            'Exec': f'/usr/bin/python3 {quote_desktop_argument(str(script))}',
            'Icon': str(icon_file) if icon_file.is_file() else app_id,
            'StartupWMClass': app_id,
            'Categories': 'System;',
        }
        for key, value in properties.items():
            entry.set_string('Desktop Entry', key, value)
        entry.set_boolean('Desktop Entry', 'Terminal', False)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            data, _ = entry.to_data()
            Gio.File.new_for_path(str(target)).replace_contents(
                data.encode('utf-8'), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None,
            )
        except (OSError, GLib.Error) as error:
            self.message('Could not install application launcher', str(error))
            return
        self.message('Application launcher installed', f'{self.win.get_title()} is registered in your application menu.')

    def shortcut(self, *_):
        desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        home = Path.home()
        folder = Path(desktop) if desktop and Path(desktop) != home else home / 'Desktop'
        target = folder / f'{APP_ID}.desktop'
        source = DATA / 'applications' / target.name
        try:
            entry = source.read_bytes()
            folder.mkdir(parents=True, exist_ok=True)
            write_file(target, entry)
            target.chmod(0o755)
        except (OSError, GLib.Error) as error:
            self.message('Could not install shortcut', str(error))
            return
        message = str(target)
        try:
            Gio.File.new_for_path(str(target)).set_attribute_string(
                'metadata::trusted', 'true', Gio.FileQueryInfoFlags.NONE, None,
            )
        except GLib.Error:
            message += '\nIf prompted, right-click it and choose Allow Launching.'
        self.message('Shortcut installed', message)

    def message(self, title, text):
        dialog = Adw.MessageDialog(transient_for=self.win, heading=title, body=text)
        dialog.add_response('ok', 'OK')
        dialog.present()

    def show_user_guide(self, *_):
        try:
            text = Path(__file__).with_name('USER-GUIDE.txt').read_text(encoding='utf-8')
        except OSError as error:
            self.message('Could not open the user guide', str(error))
            return
        window = Gtk.Window(
            title='Kiosk Studio User Guide', transient_for=self.win, modal=True,
            destroy_with_parent=True, default_width=720, default_height=600,
        )
        view = Gtk.TextView(
            editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
            left_margin=24, right_margin=24, top_margin=20, bottom_margin=20,
            pixels_below_lines=4,
        )
        view.get_buffer().set_text(text)
        window.set_child(Gtk.ScrolledWindow(child=view))
        window.present()

    def about(self, *_):
        about = Adw.AboutWindow(
            transient_for=self.win, modal=True, application_name='Kiosk Studio', application_icon=APP_ID,
            version=VERSION, developer_name=AUTHOR, developers=[AUTHOR],
            comments='Configure an approved-app launcher for an Ubuntu or Linux Mint kiosk.',
        )
        about.present()


if __name__ == '__main__':
    raise SystemExit(KioskStudio().run())
