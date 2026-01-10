project('spruce', version: '0.1.6', license: 'GPL-3.0-or-later')

py = import('python').find_installation('python3')

py_sources = files(
  'src/spruce/__init__.py',
  'src/spruce/__main__.py',
  'src/spruce/app.py'
)
py.install_sources(py_sources, subdir: 'spruce', pure: true)

install_subdir('ui', install_dir: join_paths(get_option('datadir'), 'spruce'))

install_data('data/desktop/io.github.shonubot.Spruce.desktop',
  install_dir: join_paths(get_option('datadir'), 'applications'))
install_data('data/appdata/io.github.shonubot.Spruce.metainfo.xml',
  install_dir: join_paths(get_option('datadir'), 'metainfo'))

install_data('data/icons/hicolor/scalable/apps/io.github.shonubot.Spruce.svg',
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'scalable', 'apps'))
install_data('data/icons/hicolor/symbolic/apps/io.github.shonubot.Spruce-symbolic.svg',
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'symbolic', 'apps'))

install_data('data/bin/spruce',
  install_dir : get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)

subdir('po')

