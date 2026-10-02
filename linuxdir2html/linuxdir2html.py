# Licensed GPL-3.0-only
# Copyright (C) Ali Homafar 2020-2025 https://github.com/homeisfar/LinuxDir2HTML
# Contributions Bruce Riddle 2020

# Short Changelog
# v1.3.0             - Initial release
# v1.4.0 (Aug. 2020) - Safety, logging, and --startswith and --child options.
# v1.5.0 (Oct. 2022) - Write errors fix (thanks Jarvis-3-0). Handle " in filenames.
# v1.6.0 (Dec. 2022) - Introduce the --symlink and --silent options.
# v1.6.1 (Mar. 2025) - Fixed lingering issues w.r.t. certain file names breaking the output.
#                    - Fixed issues with files or directories which do not have read rights.
# v1.7.0             - Dark mode toggle, MiB/MB toggle, access times, --hash, --date-format,
#                      and --assets to share CSS/JS between reports.
#                    - File names are HTML-escaped in the viewer.
# v1.8.0             - Created (birth) time, BLAKE3 hashing, column picker, theme follows the
#                      device setting, and a live progress counter while indexing.

# Prior to v1.6.1 symlinks filesizes were erroneously counted as the full files.
# Now symlinked files are counted for however long the symlink itself is. If the symlink
# points to a non-file then it probably will not be in the output (e.g. wine drive_c dir).
# v1.6.1 finally fixes files with new lines and * breaking the output.

import argparse
import ctypes
import ctypes.util
import datetime
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import time
import urllib.parse

try:
    import blake3          # optional: pip install blake3
except ImportError:
    blake3 = None

# Most of the following variables are to replace placeholders in template.html
appName     = "LinuxDir2HTML"
app_ver     = "1.8.0"
app_link    = "https://github.com/homeisfar/LinuxDir2HTML"
total_numFiles  = 0
total_numDirs   = 0
grand_total_size= 0
link_files      = False
link_protocol   = "file://"
include_hidden  = False
follow_symlink  = False
hash_algo       = None
date_format     = "%y/%m/%d %H:%M:%S"
dir_results     = []
childList_names = [] # names supplied from --child options
startsList_names = [] # dir's generated from --startsfrom options

# Field separator inside each entry string. The HTML parser turns NUL inside a
# <script> into U+FFFD, which is what the viewer splits on. Filenames can't contain NUL.
SEP = '\0'

# Shared-asset markers in template.html
CSS_BEGIN = '<!-- LD2H:SHARED-CSS:BEGIN -->'
CSS_END   = '<!-- LD2H:SHARED-CSS:END -->'
JS_BEGIN  = '<!-- LD2H:SHARED-JS:BEGIN -->'
JS_END    = '<!-- LD2H:SHARED-JS:END -->'

# Hash algorithms that produce a plain hexdigest (the shake_* family needs a length),
# plus BLAKE3 from the optional third-party package.
HASH_CHOICES = ['blake3'] + sorted(a for a in hashlib.algorithms_guaranteed if not a.startswith('shake_'))
DEFAULT_HASH = 'blake3' if blake3 else 'sha256'
HASH_LABELS = {'blake3': 'BLAKE3', 'md5': 'MD5', 'sha1': 'SHA-1', 'sha224': 'SHA-224', 'sha256': 'SHA-256',
               'sha384': 'SHA-384', 'sha512': 'SHA-512', 'sha3_224': 'SHA3-224',
               'sha3_256': 'SHA3-256', 'sha3_384': 'SHA3-384', 'sha3_512': 'SHA3-512',
               'blake2b': 'BLAKE2b', 'blake2s': 'BLAKE2s'}
SUPPORTED_DATE_TOKENS = set('YymdHMSb%')

parser = argparse.ArgumentParser(description='Generate HTML view of the file system.\n')
parser.add_argument('pathToIndex', help='Path of Directory to Index')
parser.add_argument('outputfile', help='Name of report file (without .html)')
parser.add_argument('--child', action='append', help='[DEPRECATED] Exact name(s) of children directories to include')
parser.add_argument('--startswith', action='append', help='[DEPRECATED] Start of name(s) of children dirs to include')
parser.add_argument('--hidden', help='Include hidden files (leading with .)', action="store_true")
parser.add_argument('--links', help='Create links to files in HTML output', action="store_true")
parser.add_argument('--symlink', help='Follow symlinks. WARN: This can cause infinite loops.', action="store_true")
parser.add_argument('--hash', nargs='?', const='default', choices=HASH_CHOICES + ['default'], metavar='ALGO',
                    help='Compute a checksum of every file (reads all file contents, so it can be slow). '
                         'ALGO defaults to blake3 if the blake3 package is installed (pip install blake3), '
                         'otherwise sha256. Choices: ' + ', '.join(HASH_CHOICES))
parser.add_argument('--date-format', default=date_format, metavar='FMT',
                    help='strftime-style format for dates in the report. Supported: %%Y %%y %%m %%d %%H %%M %%S %%b. '
                         'Default: "%(default)s", e.g. 26/10/31 23:42:12 for 31 Oct 2026')
parser.add_argument('--assets', metavar='DIR',
                    help='Write the shared CSS/JS to DIR (created if needed) and reference it from the report '
                         'instead of embedding it. Reports that use the same DIR share one copy.')
parser.add_argument('--assets-url', metavar='URL',
                    help='With --assets: URL prefix the report loads the assets from '
                         '(e.g. https://example.com/ld2h/). Default: a path relative to the report.')
parser.add_argument('-v', '--verbose', help='increase output verbosity. -v or -vv for more.', action="count")
parser.add_argument('--silent', help='Suppress terminal output except on error.', action="store_true")
parser.add_argument('--version', help='Print version and exit', action="version", version=app_ver)

def main():
    global include_hidden, link_files, childList_names, startsList_names, follow_symlink, \
            hash_algo, date_format
    args = parser.parse_args()

    ## Initialize logging facilities
    log_level = logging.WARNING
    if args.verbose:
        if args.verbose > 1:
            log_level = logging.DEBUG
        elif args.verbose == 1:
            log_level = logging.INFO
    if args.silent:
        log_level = logging.ERROR
    # Progress goes to the terminal unless --silent. Log messages clear the progress line
    # before printing so the two don't get tangled.
    progress.enabled = not args.silent
    handler = ProgressAwareHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
    logging.basicConfig(level=log_level, handlers=[handler])
    log_name = logging.getLevelName(logging.getLogger().getEffectiveLevel())
    logging.info( f'Logging Level {log_name}')

    # Handle user input flags and options
    pathToIndex = args.pathToIndex
    title = args.outputfile
    link_files = args.links
    include_hidden = args.hidden
    hash_algo = DEFAULT_HASH if args.hash == 'default' else args.hash
    if hash_algo == 'blake3' and blake3 is None:
        logging.error("--hash blake3 needs the blake3 package: pip install blake3")
        exit(1)
    date_format = args.date_format
    if args.symlink:
        follow_symlink = True
        logging.warning(f"Please be aware following symlinks can cause circular infinite loops.")
    if not os.path.exists(pathToIndex):
        logging.error(f"Directory specified to index [{pathToIndex}] doesn't exist. Aborting.")
        exit(1)
    if os.path.isdir(title):
        logging.error(f"Chosen output file [{title}] is a directory. Aborting.")
        exit(1)
    if args.assets_url and not args.assets:
        logging.error("--assets-url requires --assets. Aborting.")
        exit(1)
    unsupported = set(re.findall(r'%(.)', date_format)) - SUPPORTED_DATE_TOKENS
    if unsupported:
        logging.warning(f"--date-format: unsupported directive(s) {sorted('%' + u for u in unsupported)} "
                        "will appear literally in the report.")

    logging.info(f"Creating file links is [{link_files}]")
    logging.info(f"Showing hidden items is [{include_hidden}]")
    logging.info(f"Following symlinks is [{follow_symlink}]")
    logging.info(f"Hashing files is [{hash_algo or False}]")
    if not birthtime_supported():
        logging.warning("Creation times aren't available on this system (no statx); that column will be empty.")

    # check that no child or startswith arg include a path separator
    for child_val in args.child or []:
        if os.sep in child_val:
            logging.error(f"child argument [{child_val}] contains a path separator.")
            exit(1)
        childList_names.append(os.path.normcase(child_val))
    for start_val in args.startswith or []:
        if os.sep in start_val:
            logging.error(f"startswith argument [{start_val}] contains a path separator.")
            exit(1)
        startsList_names.append(os.path.normcase(start_val))

    # Time to do the real work. Generate array with our file & dir entries,
    # then generate the resulting HTML
    pathToIndex = Path(pathToIndex).resolve()
    logging.warning(f'Root index directory: [{pathToIndex}]')
    try:
        generateDirArray(str(pathToIndex))
    except KeyboardInterrupt:
        progress.clear()
        logging.error('Interrupted; no report written.')
        exit(130)
    progress.done()
    logging.info('Outputting HTML...')
    generateHTML(title, args.assets, args.assets_url)
    return

def human_size(n):
    for unit in ('bytes', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{int(n)} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1024

def human_time(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

class Progress:
    """Live counter on stderr. On a terminal it redraws one line ~10x per second; when
    stderr is a file or pipe (cron, logs) it prints a plain line every 15 seconds."""
    def __init__(self):
        self.enabled = True
        self.tty = sys.stderr.isatty()
        self.interval = 0.1 if self.tty else 15
        self.files = self.dirs = self.bytes = self.hashed = 0
        self.current = ''
        self.start = time.monotonic()
        self.last = 0.0
        self.showing = False

    def line(self, width=None):
        elapsed = time.monotonic() - self.start
        text = f'{human_time(elapsed)}  {self.files:,} files, {self.dirs:,} folders, {human_size(self.bytes)}'
        if hash_algo:
            rate = self.hashed / elapsed if elapsed > 0 else 0
            text += f'  |  hashed {human_size(self.hashed)} ({human_size(rate)}/s)'
        if not self.current:
            return text
        path = self.current.encode('utf-8', 'replace').decode('utf-8').replace('\n', ' ')
        if width:
            room = width - len(text) - 3
            if room < 12:
                return text[:width]
            if len(path) > room:
                path = '…' + path[-(room - 1):]
        return f'{text}  {path}'

    def update(self, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            return
        self.last = now
        if self.tty:
            width = shutil.get_terminal_size((100, 20)).columns - 1
            sys.stderr.write('\r\033[K' + self.line(width))
            self.showing = True
        else:
            sys.stderr.write(self.line() + '\n')
        sys.stderr.flush()

    def clear(self):
        if self.showing:
            sys.stderr.write('\r\033[K')
            sys.stderr.flush()
            self.showing = False

    def done(self):
        if not self.enabled:
            return
        self.clear()
        self.current = ''
        elapsed = time.monotonic() - self.start
        text = f'Indexed {self.files:,} files in {self.dirs:,} folders ({human_size(self.bytes)}) in {human_time(elapsed)}'
        if hash_algo:
            text += f', hashed {human_size(self.hashed)}'
        sys.stderr.write(text + '\n')
        sys.stderr.flush()

progress = Progress()

class ProgressAwareHandler(logging.StreamHandler):
    def emit(self, record):
        progress.clear()
        super().emit(record)

# Creation ("birth") time. Python's os.stat doesn't expose it on Linux, so call statx()
# from glibc directly (glibc 2.28+, kernel 4.11+). Filesystems that don't record it
# (FAT, many network/FUSE filesystems, /proc) return no birth time; the report leaves it blank.
_AT_FDCWD = -100
_AT_STATX_DONT_SYNC = 0x4000     # don't force a round trip on network filesystems
_STATX_BTIME = 0x800
_STATX_BTIME_OFFSET = 80         # offsetof(struct statx, stx_btime.tv_sec)
_statx = None
_statx_buf = None
try:
    _libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
    _statx = _libc.statx
    _statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    _statx.restype = ctypes.c_int
    _statx_buf = ctypes.create_string_buffer(256)   # sizeof(struct statx)
except (OSError, AttributeError):
    _statx = None

def birthtime_supported():
    return _statx is not None

def birth_time(path):
    """Creation time as an int UNIX timestamp, or None if unavailable."""
    if _statx is None:
        return None
    if _statx(_AT_FDCWD, os.fsencode(path), _AT_STATX_DONT_SYNC, _STATX_BTIME, _statx_buf) != 0:
        return None
    mask = struct.unpack_from('=I', _statx_buf, 0)[0]
    if not mask & _STATX_BTIME:
        return None
    return struct.unpack_from('=q', _statx_buf, _STATX_BTIME_OFFSET)[0]

def opt_time(t):
    return '' if t is None else str(t)

def js_str(s):
    """Escape a string for use inside a double-quoted JS string in an inline <script>."""
    # json.dumps handles quotes, backslashes, control chars and non-ASCII (including the
    # surrogate escapes Python uses for undecodable filenames). "</" is escaped so a
    # file name can never close the <script> element.
    return json.dumps(s)[1:-1].replace('</', '<\\/')

def js_json(obj):
    """JSON for embedding directly in an inline <script>."""
    return json.dumps(obj).replace('</', '<\\/')

def dir_header(path):
    """First element of a directory's array: path, size placeholder, mtime, atime, btime."""
    try:
        st = os.stat(path)
        mtime, atime = int(st.st_mtime), int(st.st_atime)
    except (OSError, OverflowError, ValueError):
        logging.warning(f'----could not stat dir [{path}]')
        mtime = atime = 0
    return SEP.join((js_str(path), '0', str(mtime), str(atime), opt_time(birth_time(path))))

def hash_file(path):
    """Return the hex digest of the file at path, or '' on error.
    Opens with O_NOATIME where permitted so hashing doesn't change the access times."""
    if hash_algo == 'blake3':
        h = blake3.blake3(max_threads=blake3.blake3.AUTO)   # multithreaded on big chunks
        chunk_size = 8 << 20
    else:
        h = hashlib.new(hash_algo)
        chunk_size = 1 << 20
    noatime = getattr(os, 'O_NOATIME', 0)
    try:
        try:
            fd = os.open(path, os.O_RDONLY | noatime)
        except PermissionError:
            if not noatime:
                raise
            fd = os.open(path, os.O_RDONLY)   # O_NOATIME requires owning the file
        with os.fdopen(fd, 'rb', buffering=0) as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                progress.hashed += len(chunk)
                progress.update()
    except OSError as e:
        logging.warning(f'----could not hash [{path}]: {e.strerror}')
        return ''
    return h.hexdigest()

def generateDirArray(root_dir): # root i.e. user-provided root path, not "/"
    global total_numFiles, total_numDirs, grand_total_size, \
            dir_results, childList_names, startsList_names
    id = 0
    dirs_dictionary = {}

    # We enumerate every unique directory, ignoring symlinks by default.
    first_iteration = True
    for current_dir, dirs, files in os.walk(root_dir, True, None, follow_symlink):
        logging.debug( f'Walking Dir [{current_dir}]')
        progress.dirs += 1
        progress.current = current_dir
        progress.update()

        # If --child or --startswith are used, only add the requested
        # directories. This will only be performed on the root_dir
        if first_iteration:
            first_iteration = False
            if childList_names or startsList_names:
                selectDirs(current_dir, dirs, include_hidden)
                files = []

        if include_hidden is False:
            dirs[:] = [d for d in dirs if not d[0] == '.']
            files = [f for f in files if not f[0] == '.']

        dirs = sorted(dirs, key=str.casefold)
        files = sorted(files, key=str.casefold)

        # The key is the current dir, and the value is described as follows.
        # A four index array like so:
        # |  0 |      1     |         2           |    3     |
        # | id | file_attrs | dir total file size | sub dirs |
        # [1] starts with the directory's header (path, mtime, atime, btime), followed by
        # one entry per file: name, size, mtime, atime, btime and, with --hash, the checksum.
        # Id is unused but could be useful for future features.
        if current_dir not in dirs_dictionary:   # only the root; subdirs are added below
            dirs_dictionary[current_dir] = [id, [dir_header(current_dir)], 0, '']
        arr = dirs_dictionary[current_dir][1]

        ##### Enumerate FILES #####
        total_size = 0
        for file in files:
            full_file_path = os.path.join(current_dir, file)
            try:
                st = os.stat(full_file_path)        # follows symlinks, like isfile()
            except OSError:
                continue                            # broken symlink, vanished file, ...
            if not os.path.isfile(full_file_path):
                continue

            is_link = os.path.islink(full_file_path)
            file_size = st.st_size
            if is_link:
                file_size = os.lstat(full_file_path).st_size
            try:  # Avoid possible invalid timestamps
                mod_time = int(st.st_mtime)
                acc_time = int(st.st_atime)
            except (OverflowError, ValueError):
                logging.warning(f'----timestamp invalid [{full_file_path}]')
                mod_time = acc_time = 1

            total_numFiles   += 1
            total_size       += file_size
            grand_total_size += file_size

            progress.files += 1
            progress.bytes += file_size

            fields = [js_str(file), str(file_size), str(mod_time), str(acc_time),
                      opt_time(birth_time(full_file_path))]
            if hash_algo:
                # The times above were read before hashing touches the file. Symlinks are
                # only hashed with --symlink; otherwise the report describes the link itself.
                digest = ''
                if not is_link or follow_symlink:
                    progress.current = full_file_path
                    digest = hash_file(full_file_path)
                    progress.current = current_dir
                fields.append(digest)
            arr.append(SEP.join(fields))
            progress.update()
        dirs_dictionary[current_dir][2] = total_size

        ##### Enumerate DIRS #####
        dir_links = []
        for dir in dirs:
            full_dir_path = os.path.join(current_dir, dir)
            if (not follow_symlink and os.path.isdir(full_dir_path) and not os.path.islink(full_dir_path)) or \
                    (follow_symlink and os.path.isdir(full_dir_path)):
                id += 1
                total_numDirs += 1
                # Header is filled in now so unreadable dirs (never walked) still show up properly
                dirs_dictionary[full_dir_path] = [id, [dir_header(full_dir_path)], 0, '']
                dir_links.append(str(id))
        dirs_dictionary[current_dir][3] = SEP.join(dir_links)

    ## Output format follows:
    # "PATH\00\0MTIME\0ATIME\0BTIME","NAME\0SIZE\0MTIME\0ATIME\0BTIME[\0HASH]",...,DIR_SIZE,"ID1\0ID2..."
    # BTIME is empty when the filesystem doesn't record creation time.
    # To get a practical sense of what this means, look at a generated output after using the program.
    for entry in dirs_dictionary:
        logging.debug(f'entry in dirs_dictionary [{str(entry)}]')
        parts = ['D.p([']
        for data in dirs_dictionary[entry][1]:
            parts.append(f'"{data}",')
        parts.append(f'{dirs_dictionary[entry][2]},"{dirs_dictionary[entry][3]}"])\n')
        dir_results.append(''.join(parts))
    return

# This function will execute only on the first iteration of the directory walk.
# It only has an effect if --child or --startswith are used.
def selectDirs(current_dir, dirs, include_hidden):
    if childList_names:
        logging.warning(f'Using dirs Named [{str(childList_names)[1:-1]}]')
    hidden_dirs = []
    if startsList_names:
        logging.warning(f'Using dirs starting with [{str(startsList_names)[1:-1]}]')
        if include_hidden:
            hidden_dirs = ["."+d for d in startsList_names]
            logging.warning(f'Hidden flag set. Using dirs starting with [{str(hidden_dirs)[1:-1]}]')

    desired_dirs = startsList_names + hidden_dirs
    for i in range(len(dirs) -1, -1, -1):
        keep_dir = '?'
        for desired in desired_dirs:
            if re.match(desired, dirs[i], re.I) or dirs[i] in childList_names:
                keep_dir = 'Y'
        if keep_dir != 'Y':
            logging.debug('..Unselecting: %s', dirs[i])
            del dirs[i]
    logging.info(f'Dirs selected:\n{dirs}')
    return

def extract_region(text, begin, end):
    """Return (before, inner, after) around the marked region of text."""
    i = text.index(begin)
    j = text.index(end, i)
    return text[:i], text[i + len(begin):j], text[j + len(end):]

def write_if_changed(path, content):
    """Write content to path unless it already holds exactly that. Returns True if written."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return True

def externalize_assets(template, output_path, assets_dir, assets_url):
    """Move the shared CSS/JS out of the template into versioned files in assets_dir
    and reference them with <link>/<script src> instead."""
    before, css_block, rest = extract_region(template, CSS_BEGIN, CSS_END)
    middle, js_block, after = extract_region(rest, JS_BEGIN, JS_END)

    css = re.search(r'<style[^>]*>(.*)</style>', css_block, re.S).group(1).strip('\n') + '\n'
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', js_block, re.S)
    js = '\n;\n'.join(s.strip('\n') for s in scripts) + '\n'

    # Versioned names, so reports made by an older version keep their matching assets
    css_name = f'linuxdir2html-{app_ver}.css'
    js_name  = f'linuxdir2html-{app_ver}.js'

    os.makedirs(assets_dir, exist_ok=True)
    for name, content in ((css_name, css), (js_name, js)):
        target = os.path.join(assets_dir, name)
        if write_if_changed(target, content):
            logging.warning(f'Wrote shared asset: {os.path.realpath(target)}')
        else:
            logging.info(f'Shared asset already up to date: {os.path.realpath(target)}')

    if assets_url:
        prefix = assets_url if assets_url.endswith('/') else assets_url + '/'
    else:
        out_dir = os.path.dirname(os.path.abspath(output_path))
        rel = os.path.relpath(os.path.abspath(assets_dir), out_dir).replace(os.sep, '/')
        prefix = '' if rel == '.' else urllib.parse.quote(rel) + '/'

    link = f'<link rel="stylesheet" href="{html.escape(prefix + css_name)}">'
    script = f'<script type="text/javascript" charset="utf-8" src="{html.escape(prefix + js_name)}"></script>'
    return f'{before}{link}{middle}{script}{after}'

def generateHTML(title, assets_dir=None, assets_url=None):
    output_path = f'{title}.html'
    with open(Path(__file__).parent / 'template.html', 'r', encoding='utf-8') as f:
        template = f.read()

    if assets_dir:
        template = externalize_assets(template, output_path, assets_dir, assets_url)

    config = {
        'numFiles': total_numFiles,
        'linkFiles': link_files,
        'linkProtocol': link_protocol,
        'dateFormat': date_format,
        'hashAlgo': hash_algo or '',
        'hashLabel': HASH_LABELS.get(hash_algo, hash_algo or ''),
    }
    replacements = {
        '[APP NAME]': appName,
        '[APP VER]': app_ver,
        '[GEN DATETIME]': html.escape(datetime.datetime.now().strftime(date_format)),
        '[TITLE]': html.escape(title),
        '[APP LINK]': app_link,
        '[NUM FILES]': str(total_numFiles),
        '[NUM DIRS]': str(total_numDirs),
        '[TOT SIZE]': str(grand_total_size),
        '[CONFIG]': js_json(config),
    }
    for key, val in replacements.items():
        template = template.replace(key, val)

    head, tail = template.split('[DIR DATA]', 1)
    with open(output_path, 'w', encoding='utf-8', errors='xmlcharrefreplace') as out:
        out.write(head)
        for line in dir_results:
            try:  # can error if encoding mismatch; can't fix, just report
                out.write(line)
            except Exception:
                logging.warning(f'----output_file.write error [{line}]')
        out.write(tail)
    logging.warning("Wrote output to: " + os.path.realpath(output_path))
    return

if __name__ == '__main__':
    main()
