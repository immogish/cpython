#!/usr/bin/env python
"""
скрипт для сборки unix-like дистрибутива python
со всеми необходимыми модулями (openssl, tcl/tk, sqlite3 и пр.)
"""
import platform, os, sys, getopt, textwrap, shutil, stat, time, pwd, grp
try:
    import urllib2 as urllib_request
except ImportError:
    import urllib.request as urllib_request

STAT_0o755 = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
             | stat.S_IRGRP |                stat.S_IXGRP
             | stat.S_IROTH |                stat.S_IXOTH )

STAT_0o775 = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
             | stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP
             | stat.S_IROTH |                stat.S_IXOTH )

INCLUDE_TIMESTAMP = 1
VERBOSE = 1

RUNNING_ON_PYTHON2 = sys.version_info.major == 2

if RUNNING_ON_PYTHON2:
    from plistlib import writePlist
else:
    from plistlib import dump
    def writePlist(path, plist):
        with open(plist, 'wb') as fp:
            dump(path, fp)

def shellQuote(value):
    """
    Return the string value in a form that can safely be inserted into
    a shell command.
    """
    return "'%s'"%(value.replace("'", "'\"'\"'"))

def grepValue(fn, variable):
    """
    Return the unquoted value of a variable from a file..
    QUOTED_VALUE='quotes'    -> str('quotes')
    UNQUOTED_VALUE=noquotes  -> str('noquotes')
    """
    variable = variable + '='
    for ln in open(fn, 'r'):
        if ln.startswith(variable):
            value = ln[len(variable):].strip()
            return value.strip("\"'")
    raise RuntimeError("Cannot find variable %s" % variable[:-1])

_cache_getVersion = None

def getVersion():
    global _cache_getVersion
    if _cache_getVersion is None:
        _cache_getVersion = grepValue(
            os.path.join(SRCDIR, 'configure'), 'PACKAGE_VERSION')
    return _cache_getVersion

def getVersionMajorMinor():
    return tuple([int(n) for n in getVersion().split('.', 2)])

_cache_getFullVersion = None

def getFullVersion():
    global _cache_getFullVersion
    if _cache_getFullVersion is not None:
        return _cache_getFullVersion
    fn = os.path.join(SRCDIR, 'Include', 'patchlevel.h')
    for ln in open(fn):
        if 'PY_VERSION' in ln:
            _cache_getFullVersion = ln.split()[-1][1:-1]
            return _cache_getFullVersion
    raise RuntimeError("Cannot find full version??")

FW_PREFIX = ["Library", "Frameworks", "Python.framework"]
FW_VERSION_PREFIX = "--undefined--" # initialized in parseOptions
FW_SSL_DIRECTORY = "--undefined--" # initialized in parseOptions

# The directory we'll use to create the build (will be erased and recreated)
WORKDIR = "/tmp/_py_embed"
OUTDIR = os.path.join(WORKDIR, '_out')

# The directory we'll use to store third-party sources. Set this to something
# else if you don't want to re-fetch required libraries every time.
DEPSRC = os.path.join(WORKDIR, 'third-party')
DEPSRC = os.path.expanduser('~/Universal/other-sources')

universal_opts_map = { 'universal2': ('arm64', 'x86_64'),
                       '32-bit': ('i386', 'ppc',),
                       '64-bit': ('x86_64', 'ppc64',),
                       'intel':  ('i386', 'x86_64'),
                       'intel-32':  ('i386',),
                       'intel-64':  ('x86_64',),
                       '3-way':  ('ppc', 'i386', 'x86_64'),
                       'all':    ('i386', 'ppc', 'x86_64', 'ppc64',) }
default_target_map = {
        'universal2': '10.9',
        '64-bit': '10.5',
        '3-way': '10.5',
        'intel': '10.5',
        'intel-32': '10.4',
        'intel-64': '10.5',
        'all': '10.5',
}

UNIVERSALOPTS = tuple(universal_opts_map.keys())

UNIVERSALARCHS = '32-bit'

ARCHLIST = universal_opts_map[UNIVERSALARCHS]

# Source directory (assume we're in Mac/BuildScript)
SRCDIR = os.path.dirname(
        os.path.dirname(
            os.path.dirname(
                os.path.abspath(__file__
        ))))

# $MACOSX_DEPLOYMENT_TARGET -> minimum OS X level
DEPTARGET = '10.5'

def getDeptargetTuple():
    return tuple([int(n) for n in DEPTARGET.split('.')[0:2]])

def getBuildTuple():
    return tuple([int(n) for n in platform.mac_ver()[0].split('.')[0:2]])

def getTargetCompilers():
    target_cc_map = {
        '10.4': ('gcc-4.0', 'g++-4.0'),
        '10.5': ('gcc', 'g++'),
        '10.6': ('gcc', 'g++'),
    }
    return target_cc_map.get(DEPTARGET, ('gcc', 'g++') )

CC, CXX = getTargetCompilers()

PYTHON_3 = getVersionMajorMinor() >= (3, 0)

USAGE = textwrap.dedent("""\
    Usage: build_python [options]

    Options:
    -? or -h:            Show this message
    -b DIR
    --build-dir=DIR:     Create build here (default: %(WORKDIR)r)
    --third-party=DIR:   Store third-party sources here (default: %(DEPSRC)r)
    --sdk-path=DIR:      Location of the SDK (deprecated, use SDKROOT env variable)
    --src-dir=DIR:       Location of the Python sources (default: %(SRCDIR)r)
    --dep-target=10.n    macOS deployment target (default: %(DEPTARGET)r)
    --universal-archs=x  universal architectures (options: %(UNIVERSALOPTS)r, default: %(UNIVERSALARCHS)r)
""")% globals()

# Dict of object file names with shared library names to check after building.
# This is to ensure that we ended up dynamically linking with the shared
# library paths and versions we expected.  For example:
#   EXPECTED_SHARED_LIBS['_tkinter.so'] = [
#                       '/Library/Frameworks/Tcl.framework/Versions/8.5/Tcl',
#                       '/Library/Frameworks/Tk.framework/Versions/8.5/Tk']
EXPECTED_SHARED_LIBS = {}

# Are we building and linking with our own copy of Tcl/TK?
#   For now, do so if deployment target is 10.6+.
def internalTk():
    return getDeptargetTuple() >= (10, 6)

# Do we use 8.6.8 when building our own copy
# of Tcl/Tk or a modern version.
#   We use the old version when buildin on
#   old versions of macOS due to build issues.
def useOldTk():
    return getBuildTuple() < (10, 15)


def tweak_tcl_build(basedir, archList):
    with open("Makefile", "r") as fp:
        contents = fp.readlines()

    # For reasons I don't understand the tcl configure script
    # decides that some stdlib symbols aren't present, before
    # deciding that strtod is broken.
    new_contents = []
    for line in contents:
        if line.startswith("COMPAT_OBJS"):
            # note: the space before strtod.o is intentional,
            # the detection of a broken strtod results in
            # "fixstrod.o" on this line.
            for nm in ("strstr.o", "strtoul.o", " strtod.o"):
                line = line.replace(nm, "")
        new_contents.append(line)

    with open("Makefile", "w") as fp:
        fp.writelines(new_contents)

# List of names of third party software built with this installer.
# The names will be inserted into the rtf version of the License.
THIRD_PARTY_LIBS = []

# Instructions for building libraries that are necessary for building a
# batteries included python.
#   [The recipes are defined here for convenience but instantiated later after
#    command line options have been processed.]
def library_recipes():
    result = []

    # Since Apple removed the header files for the deprecated system
    # OpenSSL as of the Xcode 7 release (for OS X 10.10+), we do not
    # have much choice but to build our own copy here, too.

    result.extend([
          dict(
              name="OpenSSL 1.1.1n",
              url="https://www.openssl.org/source/openssl-1.1.1n.tar.gz",
              checksum='2aad5635f9bb338bc2c6b7d19cbc9676',
              buildrecipe=build_universal_openssl,
              configure=None,
              install=None,
          ),
    ])

    if internalTk():
        if useOldTk():
            tcl_tk_ver='8.6.8'
            tcl_checksum='81656d3367af032e0ae6157eff134f89'

            tk_checksum='5e0faecba458ee1386078fb228d008ba'
            tk_patches = ['tk868_on_10_8_10_9.patch']

        else:
            tcl_tk_ver='8.6.12'
            tcl_checksum='87ea890821d2221f2ab5157bc5eb885f'

            tk_checksum='1d6dcf6120356e3d211e056dff5e462a'
            tk_patches = [ ]


        result.extend([
          dict(
              name="Tcl %s"%(tcl_tk_ver,),
              url="https://prdownloads.sourceforge.net/tcl/tcl%s-src.tar.gz"%(tcl_tk_ver,),
              checksum=tcl_checksum,
              buildDir="unix",
              configure_pre=[
                    '--enable-shared',
                    '--enable-threads',
                    '--libdir=%s/lib' % OUTDIR,
              ],
              useLDFlags=False,
              buildrecipe=tweak_tcl_build,
              install='make TCL_LIBRARY=%(TCL_LIBRARY)s && make install TCL_LIBRARY=%(TCL_LIBRARY)s DESTDIR=%(DESTDIR)s'%{
                  "DESTDIR": shellQuote(os.path.join(WORKDIR, 'libraries')),
                  "TCL_LIBRARY": shellQuote('%s/lib/tcl8.6' % OUTDIR),
                  },
              ),
          dict(
              name="Tk %s"%(tcl_tk_ver,),
              url="https://prdownloads.sourceforge.net/tcl/tk%s-src.tar.gz"%(tcl_tk_ver,),
              checksum=tk_checksum,
              patches=tk_patches,
              buildDir="unix",
              configure_pre=[
                    '--enable-aqua',
                    '--enable-shared',
                    '--enable-threads',
                    '--libdir=%s/lib' % OUTDIR,
              ],
              useLDFlags=False,
              install='make TCL_LIBRARY=%(TCL_LIBRARY)s TK_LIBRARY=%(TK_LIBRARY)s && make install TCL_LIBRARY=%(TCL_LIBRARY)s TK_LIBRARY=%(TK_LIBRARY)s DESTDIR=%(DESTDIR)s'%{
                  "DESTDIR": shellQuote(os.path.join(WORKDIR, 'libraries')),
                  "TCL_LIBRARY": shellQuote('%s/lib/tcl8.6' % OUTDIR),
                  "TK_LIBRARY": shellQuote('%s/lib/tk8.6' % OUTDIR),
                  },
          ),
        ])
    if PYTHON_3:
        result.extend([
          dict(
              name="XZ 5.2.3",
              url="http://tukaani.org/xz/xz-5.2.3.tar.gz",
              checksum='ef68674fb47a8b8e741b34e429d86e9d',
              configure_pre=[
                    '--disable-dependency-tracking',
              ]
              ),
        ])

    result.extend([
          dict(
              name="NCurses 5.9",
              url="http://ftp.gnu.org/pub/gnu/ncurses/ncurses-5.9.tar.gz",
              checksum='8cb9c412e5f2d96bc6f459aa8c6282a1',
              configure_pre=[
                  "--enable-widec",
                  "--without-cxx",
                  "--without-cxx-binding",
                  "--without-ada",
                  "--without-curses-h",
                  "--enable-shared",
                  "--with-shared",
                  "--without-debug",
                  "--without-normal",
                  "--without-tests",
                  "--without-manpages",
                  "--datadir=/usr/share",
                  "--sysconfdir=/etc",
                  "--sharedstatedir=/usr/com",
                  "--with-terminfo-dirs=/usr/share/terminfo",
                  "--with-default-terminfo-dir=/usr/share/terminfo",
                  "--libdir=%s/lib"%(OUTDIR,),
              ],
              patchscripts=[
                  ("ftp://ftp.invisible-island.net/ncurses//5.9/ncurses-5.9-20120616-patch.sh.bz2",
                   "f54bf02a349f96a7c4f0d00922f3a0d4"),
                   ],
              useLDFlags=False,
              install='make && make install DESTDIR=%s'%(
                  shellQuote(os.path.join(WORKDIR, 'libraries')),
                  ),
          ),
          dict(
              name="SQLite 3.37.2",
              url="https://sqlite.org/2022/sqlite-autoconf-3370200.tar.gz",
              checksum='683cc5312ee74e71079c14d24b7a6d27',
              extra_cflags=('-Os '
                            '-DSQLITE_ENABLE_FTS5 '
                            '-DSQLITE_ENABLE_FTS4 '
                            '-DSQLITE_ENABLE_FTS3_PARENTHESIS '
                            '-DSQLITE_ENABLE_JSON1 '
                            '-DSQLITE_ENABLE_RTREE '
                            '-DSQLITE_TCL=0 '
                            ),
              configure_pre=[
                  '--enable-threadsafe',
                  '--enable-shared=no',
                  '--enable-static=yes',
                  '--disable-readline',
                  '--disable-dependency-tracking',
              ],
          ),
        ])

    if not PYTHON_3:
        result.extend([
          dict(
              name="Sleepycat DB 4.7.25",
              url="http://download.oracle.com/berkeley-db/db-4.7.25.tar.gz",
              checksum='ec2b87e833779681a0c3a814aa71359e',
              buildDir="build_unix",
              configure="../dist/configure",
              configure_pre=[
                  '--includedir=/usr/local/include/db4',
              ],
          ),
        ])

    return result


def fatal(msg):
    """
    A fatal error, bail out.
    """
    sys.stderr.write('FATAL: ')
    sys.stderr.write(msg)
    sys.stderr.write('\n')
    sys.exit(1)

def fileContents(fn):
    """
    Return the contents of the named file
    """
    return open(fn, 'r').read()

def runCommand(commandline):
    """
    Run a command and raise RuntimeError if it fails. Output is suppressed
    unless the command fails.
    """
    print("========\n" + commandline + "\n========\n")
    fd = os.popen(commandline, 'r')
    data = fd.read()
    xit = fd.close()
    if xit is not None:
        sys.stdout.write(data)
        raise RuntimeError("command failed: %s"%(commandline,))

    if VERBOSE:
        sys.stdout.write(data); sys.stdout.flush()

def captureCommand(commandline):
    fd = os.popen(commandline, 'r')
    data = fd.read()
    xit = fd.close()
    if xit is not None:
        sys.stdout.write(data)
        raise RuntimeError("command failed: %s"%(commandline,))

    return data

def getTclTkVersion(configfile, versionline):
    """
    search Tcl or Tk configuration file for version line
    """
    try:
        f = open(configfile, "r")
    except OSError:
        fatal("Framework configuration file not found: %s" % configfile)

    for l in f:
        if l.startswith(versionline):
            f.close()
            return l

    fatal("Version variable %s not found in framework configuration file: %s"
            % (versionline, configfile))

def checkEnvironment():
    """
    Check that we're running on a supported system.
    """

    if sys.version_info[0:2] < (2, 7):
        fatal("This script must be run with Python 2.7 (or later)")

    if platform.system() != 'Darwin':
        fatal("This script should be run on a macOS 10.5 (or later) system")

    if int(platform.release().split('.')[0]) < 8:
        fatal("This script should be run on a macOS 10.5 (or later) system")

    # Because we only support dynamic load of only one major/minor version of
    # Tcl/Tk, if we are not using building and using our own private copy of
    # Tcl/Tk, ensure:
    # 1. there is a user-installed framework (usually ActiveTcl) in (or linked
    #       in) SDKROOT/Library/Frameworks.  As of Python 3.7.0, we no longer
    #       enforce that the version of the user-installed framework also
    #       exists in the system-supplied Tcl/Tk frameworks.  Time to support
    #       Tcl/Tk 8.6 even if Apple does not.
    if not internalTk():
        frameworks = {}
        for framework in ['Tcl', 'Tk']:
            fwpth = 'Library/Frameworks/%s.framework/Versions/Current' % framework
            libfw = os.path.join('/', fwpth)
            usrfw = os.path.join(os.getenv('HOME'), fwpth)
            frameworks[framework] = os.readlink(libfw)
            if not os.path.exists(libfw):
                fatal("Please install a link to a current %s %s as %s so "
                        "the user can override the system framework."
                        % (framework, frameworks[framework], libfw))
            if os.path.exists(usrfw):
                fatal("Please rename %s to avoid possible dynamic load issues."
                        % usrfw)

        if frameworks['Tcl'] != frameworks['Tk']:
            fatal("The Tcl and Tk frameworks are not the same version.")

        print(" -- Building with external Tcl/Tk %s frameworks"
                    % frameworks['Tk'])

        # add files to check after build
        EXPECTED_SHARED_LIBS['_tkinter.so'] = [
                "/Library/Frameworks/Tcl.framework/Versions/%s/Tcl"
                    % frameworks['Tcl'],
                "/Library/Frameworks/Tk.framework/Versions/%s/Tk"
                    % frameworks['Tk'],
                ]
    else:
        print(" -- Building private copy of Tcl/Tk")
    print("")

    # Remove inherited environment variables which might influence build
    environ_var_prefixes = ['CPATH', 'C_INCLUDE_', 'DYLD_', 'LANG', 'LC_',
                            'LD_', 'LIBRARY_', 'PATH', 'PYTHON']
    for ev in list(os.environ):
        for prefix in environ_var_prefixes:
            if ev.startswith(prefix) :
                print("INFO: deleting environment variable %s=%s" % (
                                                    ev, os.environ[ev]))
                del os.environ[ev]

    base_path = '/bin:/sbin:/usr/bin:/usr/sbin'
    if 'SDK_TOOLS_BIN' in os.environ:
        base_path = os.environ['SDK_TOOLS_BIN'] + ':' + base_path
    # Xcode 2.5 on OS X 10.4 does not include SetFile in its usr/bin;
    # add its fixed location here if it exists
    OLD_DEVELOPER_TOOLS = '/Developer/Tools'
    if os.path.isdir(OLD_DEVELOPER_TOOLS):
        base_path = base_path + ':' + OLD_DEVELOPER_TOOLS
    os.environ['PATH'] = base_path
    print("Setting default PATH: %s"%(os.environ['PATH']))

def parseOptions(args=None):
    """
    Parse arguments and update global settings.
    """
    global WORKDIR, DEPSRC, SRCDIR, DEPTARGET
    global UNIVERSALOPTS, UNIVERSALARCHS, ARCHLIST, CC, CXX
    global FW_VERSION_PREFIX
    global FW_SSL_DIRECTORY

    if args is None:
        args = sys.argv[1:]

    try:
        options, args = getopt.getopt(args, '?hb',
                [ 'build-dir=', 'third-party=', 'sdk-path=' , 'src-dir=',
                  'dep-target=', 'universal-archs=', 'help' ])
    except getopt.GetoptError:
        print(sys.exc_info()[1])
        sys.exit(1)

    if args:
        print("Additional arguments")
        sys.exit(1)

    deptarget = None
    for k, v in options:
        if k in ('-h', '-?', '--help'):
            print(USAGE)
            sys.exit(0)

        elif k in ('-d', '--build-dir'):
            WORKDIR=v

        elif k in ('--third-party',):
            DEPSRC=v

        elif k in ('--sdk-path',):
            print(" WARNING: --sdk-path is no longer supported")

        elif k in ('--src-dir',):
            SRCDIR=v

        elif k in ('--dep-target', ):
            DEPTARGET=v
            deptarget=v

        elif k in ('--universal-archs', ):
            if v in UNIVERSALOPTS:
                UNIVERSALARCHS = v
                ARCHLIST = universal_opts_map[UNIVERSALARCHS]
                if deptarget is None:
                    # Select alternate default deployment
                    # target
                    DEPTARGET = default_target_map.get(v, '10.5')
            else:
                raise NotImplementedError(v)

        else:
            raise NotImplementedError(k)

    SRCDIR=os.path.abspath(SRCDIR)
    WORKDIR=os.path.abspath(WORKDIR)
    DEPSRC=os.path.abspath(DEPSRC)

    CC, CXX = getTargetCompilers()

    FW_VERSION_PREFIX = FW_PREFIX[:] + ["Versions", getVersion()]
    FW_SSL_DIRECTORY = FW_VERSION_PREFIX[:] + ["etc", "openssl"]

    print("-- Settings:")
    print("   * Source directory:    %s" % SRCDIR)
    print("   * Build directory:     %s" % WORKDIR)
    print("   * Third-party source:  %s" % DEPSRC)
    print("   * Deployment target:   %s" % DEPTARGET)
    print("   * Universal archs:     %s" % str(ARCHLIST))
    print("   * C compiler:          %s" % CC)
    print("   * C++ compiler:        %s" % CXX)
    print("")
    print(" -- Building a Python %s embed at patch level %s"
                % (getVersion(), getFullVersion()))
    print("")

def extractArchive(builddir, archiveName):
    """
    Extract a source archive into 'builddir'. Returns the path of the
    extracted archive.

    XXX: This function assumes that archives contain a toplevel directory
    that is has the same name as the basename of the archive. This is
    safe enough for almost anything we use.  Unfortunately, it does not
    work for current Tcl and Tk source releases where the basename of
    the archive ends with "-src" but the uncompressed directory does not.
    For now, just special case Tcl and Tk tar.gz downloads.
    """
    curdir = os.getcwd()
    try:
        os.chdir(builddir)
        if archiveName.endswith('.tar.gz'):
            retval = os.path.basename(archiveName[:-7])
            if ((retval.startswith('tcl') or retval.startswith('tk'))
                    and retval.endswith('-src')):
                retval = retval[:-4]
            if os.path.exists(retval):
                shutil.rmtree(retval)
            fp = os.popen("tar zxf %s 2>&1"%(shellQuote(archiveName),), 'r')

        elif archiveName.endswith('.tar.bz2'):
            retval = os.path.basename(archiveName[:-8])
            if os.path.exists(retval):
                shutil.rmtree(retval)
            fp = os.popen("tar jxf %s 2>&1"%(shellQuote(archiveName),), 'r')

        elif archiveName.endswith('.tar'):
            retval = os.path.basename(archiveName[:-4])
            if os.path.exists(retval):
                shutil.rmtree(retval)
            fp = os.popen("tar xf %s 2>&1"%(shellQuote(archiveName),), 'r')

        elif archiveName.endswith('.zip'):
            retval = os.path.basename(archiveName[:-4])
            if os.path.exists(retval):
                shutil.rmtree(retval)
            fp = os.popen("unzip %s 2>&1"%(shellQuote(archiveName),), 'r')

        data = fp.read()
        xit = fp.close()
        if xit is not None:
            sys.stdout.write(data)
            raise RuntimeError("Cannot extract %s"%(archiveName,))

        return os.path.join(builddir, retval)

    finally:
        os.chdir(curdir)

def downloadURL(url, fname):
    """
    Download the contents of the url into the file.
    """
    fpIn = urllib_request.urlopen(url)
    fpOut = open(fname, 'wb')
    block = fpIn.read(10240)
    try:
        while block:
            fpOut.write(block)
            block = fpIn.read(10240)
        fpIn.close()
        fpOut.close()
    except:
        try:
            os.unlink(fname)
        except OSError:
            pass

def verifyThirdPartyFile(url, checksum, fname):
    """
    Download file from url to filename fname if it does not already exist.
    Abort if file contents does not match supplied md5 checksum.
    """
    name = os.path.basename(fname)
    if os.path.exists(fname):
        print("Using local copy of %s"%(name,))
    else:
        print("Did not find local copy of %s"%(name,))
        print("Downloading %s"%(name,))
        downloadURL(url, fname)
        print("Archive for %s stored as %s"%(name, fname))
    if os.system(
            'MD5=$(openssl md5 %s) ; test "${MD5##*= }" = "%s"'
                % (shellQuote(fname), checksum) ):
        fatal('MD5 checksum mismatch for file %s' % fname)

def build_universal_openssl(basedir, archList):
    """
    Special case build recipe for universal build of openssl.

    The upstream OpenSSL build system does not directly support
    OS X universal builds.  We need to build each architecture
    separately then lipo them together into fat libraries.
    """

    # OpenSSL fails to build with Xcode 2.5 (on OS X 10.4).
    # If we are building on a 10.4.x or earlier system,
    # unilaterally disable assembly code building to avoid the problem.
    no_asm = int(platform.release().split(".")[0]) < 9

    def build_openssl_arch(archbase, arch):
        "Build one architecture of openssl"
        arch_opts = {
            "i386": ["darwin-i386-cc"],
            "x86_64": ["darwin64-x86_64-cc", "enable-ec_nistp_64_gcc_128"],
            "arm64": ["darwin64-arm64-cc"],
            "ppc": ["darwin-ppc-cc"],
            "ppc64": ["darwin64-ppc-cc"],
        }

        # Somewhere between OpenSSL 1.1.0j and 1.1.1c, changes cause the
        # "enable-ec_nistp_64_gcc_128" option to get compile errors when
        # building on our 10.6 gcc-4.2 environment.  There have been other
        # reports of projects running into this when using older compilers.
        # So, for now, do not try to use "enable-ec_nistp_64_gcc_128" when
        # building for 10.6.
        if getDeptargetTuple() == (10, 6):
            arch_opts['x86_64'].remove('enable-ec_nistp_64_gcc_128')

        configure_opts = [
            "no-idea",
            "no-mdc2",
            "no-rc5",
            "no-zlib",
            "no-ssl3",
            # "enable-unit-test",
            "shared",
            "--prefix=%s"%os.path.join(OUTDIR),
            "--openssldir=%s"%os.path.join(OUTDIR),
        ]
        if no_asm:
            configure_opts.append("no-asm")
        runCommand(" ".join(["perl", "Configure"]
                        + arch_opts[arch] + configure_opts))
        runCommand("make depend")
        runCommand("make all")
        runCommand("make install_sw DESTDIR=%s"%shellQuote(archbase))
        # runCommand("make test")
        return

    srcdir = os.getcwd()
    universalbase = os.path.join(srcdir, "..",
                        os.path.basename(srcdir) + "-universal")
    os.mkdir(universalbase)
    archbases = []
    for arch in archList:
        # fresh copy of the source tree
        archsrc = os.path.join(universalbase, arch, "src")
        shutil.copytree(srcdir, archsrc, symlinks=True)
        # install base for this arch
        archbase = os.path.join(universalbase, arch, "root")
        os.mkdir(archbase)

        # build one architecture
        os.chdir(archsrc)
        build_openssl_arch(archbase, arch)
        os.chdir(srcdir)
        archbases.append(archbase)

    # copy arch-independent files from last build into the basedir framework
    base = os.path.join(basedir)
    shutil.copytree(
            os.path.join(archbase, *OUTDIR.split("/"), "include", "openssl"),
            os.path.join(base, *OUTDIR.split(os.path.sep), "include", "openssl")
            )

    shlib_version_number = grepValue(os.path.join(archsrc, "Makefile"),
            "SHLIB_VERSION_NUMBER")
    #   e.g. -> "1.0.0"
    libcrypto = "libcrypto.dylib"
    libcrypto_versioned = libcrypto.replace(".", "."+shlib_version_number+".")
    #   e.g. -> "libcrypto.1.0.0.dylib"
    libssl = "libssl.dylib"
    libssl_versioned = libssl.replace(".", "."+shlib_version_number+".")
    #   e.g. -> "libssl.1.0.0.dylib"

    paths_to_create = [
        os.path.join(base, *OUTDIR.split("/"), "lib"),
        #os.path.join(OUTDIR, "lib"),
        #os.path.join(OUTDIR, "include")
    ]
    for path in paths_to_create:
        try:
            os.mkdir(path)
        except OSError:
            pass

    # merge the individual arch-dependent shared libs into a fat shared lib
    archbases.insert(0, base)
    for (lib_unversioned, lib_versioned) in [
                (libcrypto, libcrypto_versioned),
                (libssl, libssl_versioned)
            ]:
        runCommand("lipo -create -output " +
                   " ".join(shellQuote(
                            os.path.join(fw, *OUTDIR.split("/"), "lib", lib_versioned))
                            for fw in archbases))
        # and create an unversioned symlink of it
        os.symlink(lib_versioned, os.path.join(base, *OUTDIR.split("/"), "lib", lib_unversioned))

    # Create links in the temp include and lib dirs that will be injected
    # into the Python build so that setup.py can find them while building
    # and the versioned links so that the setup.py post-build import test
    # does not fail.
    relative_path = os.path.join("..", "..", "..", *OUTDIR.split("/"))
    for fn in [
            ["include", "openssl"],
            ["lib", libcrypto],
            ["lib", libssl],
            ["lib", libcrypto_versioned],
            ["lib", libssl_versioned],
        ]:
        os.symlink(
            os.path.join(relative_path, *fn),
            os.path.join(basedir, "usr", "local", *fn)
        )

    return

def buildRecipe(recipe, basedir, archList):
    """
    Build software using a recipe. This function does the
    'configure;make;make install' dance for C software, with a possibility
    to customize this process, basically a poor-mans DarwinPorts.
    """
    curdir = os.getcwd()

    name = recipe['name']
    THIRD_PARTY_LIBS.append(name)
    url = recipe['url']
    configure = recipe.get('configure', './configure')
    buildrecipe = recipe.get('buildrecipe', None)
    install = recipe.get('install', 'make && make install DESTDIR=%s'%(
        shellQuote(basedir)))

    archiveName = os.path.split(url)[-1]
    sourceArchive = os.path.join(DEPSRC, archiveName)

    if not os.path.exists(DEPSRC):
        os.mkdir(DEPSRC)

    verifyThirdPartyFile(url, recipe['checksum'], sourceArchive)
    print("Extracting archive for %s"%(name,))
    buildDir=os.path.join(WORKDIR, '_bld')
    if not os.path.exists(buildDir):
        os.mkdir(buildDir)

    workDir = extractArchive(buildDir, sourceArchive)
    os.chdir(workDir)

    for patch in recipe.get('patches', ()):
        if isinstance(patch, tuple):
            url, checksum = patch
            fn = os.path.join(DEPSRC, os.path.basename(url))
            verifyThirdPartyFile(url, checksum, fn)
        else:
            # patch is a file in the source directory
            fn = os.path.join(curdir, patch)
        runCommand('patch -p%s < %s'%(recipe.get('patchlevel', 1),
            shellQuote(fn),))

    for patchscript in recipe.get('patchscripts', ()):
        if isinstance(patchscript, tuple):
            url, checksum = patchscript
            fn = os.path.join(DEPSRC, os.path.basename(url))
            verifyThirdPartyFile(url, checksum, fn)
        else:
            # patch is a file in the source directory
            fn = os.path.join(curdir, patchscript)
        if fn.endswith('.bz2'):
            runCommand('bunzip2 -fk %s' % shellQuote(fn))
            fn = fn[:-4]
        runCommand('sh %s' % shellQuote(fn))
        os.unlink(fn)

    if 'buildDir' in recipe:
        os.chdir(recipe['buildDir'])

    if configure is not None:
        configure_args = [
            "--prefix=/usr/local",
            "--enable-static",
            "--disable-shared",
            #"CPP=gcc -arch %s -E"%(' -arch '.join(archList,),),
        ]

        if 'configure_pre' in recipe:
            args = list(recipe['configure_pre'])
            if '--disable-static' in args:
                configure_args.remove('--enable-static')
            if '--enable-shared' in args:
                configure_args.remove('--disable-shared')
            configure_args.extend(args)

        if recipe.get('useLDFlags', 1):
            configure_args.extend([
                "CFLAGS=%s-mmacosx-version-min=%s -arch %s "
                            "-I%s/usr/local/include"%(
                        recipe.get('extra_cflags', ''),
                        DEPTARGET,
                        ' -arch '.join(archList),
                        shellQuote(basedir)[1:-1],),
                "LDFLAGS=-mmacosx-version-min=%s -L%s/usr/local/lib -arch %s"%(
                    DEPTARGET,
                    shellQuote(basedir)[1:-1],
                    ' -arch '.join(archList)),
            ])
        else:
            configure_args.extend([
                "CFLAGS=%s-mmacosx-version-min=%s -arch %s "
                            "-I%s/usr/local/include"%(
                        recipe.get('extra_cflags', ''),
                        DEPTARGET,
                        ' -arch '.join(archList),
                        shellQuote(basedir)[1:-1],),
            ])

        if 'configure_post' in recipe:
            configure_args = configure_args + list(recipe['configure_post'])

        configure_args.insert(0, configure)
        configure_args = [ shellQuote(a) for a in configure_args ]

        print("Running configure for %s"%(name,))
        runCommand(' '.join(configure_args) + ' 2>&1')

    if buildrecipe is not None:
        # call special-case build recipe, e.g. for openssl
        buildrecipe(basedir, archList)

    if install is not None:
        print("Running install for %s"%(name,))
        runCommand('{ ' + install + ' ;} 2>&1')

    print("Done %s"%(name,))
    print("")

    os.chdir(curdir)

def buildLibraries():
    """
    Build our dependencies into $WORKDIR/libraries/usr/local
    """
    print("")
    print("Building required libraries")
    print("")
    universal = os.path.join(WORKDIR, 'libraries')
    os.mkdir(universal)
    os.makedirs(os.path.join(universal, 'usr', 'local', 'lib'))
    os.makedirs(os.path.join(universal, 'usr', 'local', 'include'))
    for recipe in library_recipes():
        buildRecipe(recipe, universal, ARCHLIST)
    libraries_postbuild()


def libraries_postbuild():

    libs_path = os.path.join(WORKDIR, 'libraries')

    if os.path.exists(libs_path):
        print("Copying required shared libraries & includes")
        build_lib_dir = os.path.join(libs_path, *OUTDIR.split("/"))
        shutil.copytree(build_lib_dir, OUTDIR, symlinks=True)

    # create directory for OpenSSL certificates
    ssl_dir = os.path.join(OUTDIR, 'etc', 'openssl')
    os.makedirs(ssl_dir)

def buildPython():
    print("Building a universal python for %s architectures" % UNIVERSALARCHS)

    buildDir = os.path.join(WORKDIR, '_bld', 'python')
    rootDir = OUTDIR

    if os.path.exists(buildDir):
        shutil.rmtree(buildDir)

    os.makedirs(buildDir)
    os.chdir(buildDir)

    # Since the extra libs are not in their installed framework location
    # during the build, augment the library path so that the interpreter
    # will find them during its extension import sanity checks.

    print("Running configure...")
    runCommand("%s -C --enable-universalsdk=/ "
               "--with-universal-archs=%s "
               "--prefix=%s "
               "%s "
               "%s "
               "%s "
               "%s "
               "%s "
               "--enable-shared "
               "LDFLAGS='-g -L%s/libraries/usr/local/lib' "
               "CFLAGS='-g -I%s/libraries/usr/local/include' 2>&1" % (
        shellQuote(os.path.join(SRCDIR, 'configure')),
        UNIVERSALARCHS,
        rootDir,
        (' ', '--with-computed-gotos ')[PYTHON_3],
        (' ', '--with-ensurepip=install ')[PYTHON_3],
        (' ', "--with-openssl='%s/libraries/usr/local'"%(
                            shellQuote(WORKDIR)[1:-1],))[PYTHON_3],
        (' ', "--with-tcltk-includes='-I%s/libraries/usr/local/include'"%(
                            shellQuote(WORKDIR)[1:-1],))[internalTk()],
        (' ', "--with-tcltk-libs='-L%s/libraries/usr/local/lib -ltcl8.6 -ltk8.6'"%(
                            shellQuote(WORKDIR)[1:-1],))[internalTk()],
        shellQuote(WORKDIR)[1:-1],
        shellQuote(WORKDIR)[1:-1]))

    # As of macOS 10.11 with SYSTEM INTEGRITY PROTECTION, DYLD_*
    # environment variables are no longer automatically inherited
    # by child processes from their parents. We used to just set
    # DYLD_LIBRARY_PATH, pointing to the third-party libs,
    # in build-installer.py's process environment and it was
    # passed through the make utility into the environment of
    # setup.py. Instead, we now append DYLD_LIBRARY_PATH to
    # the existing RUNSHARED configuration value when we call
    # make for extension module builds.

    runshared_for_make = "".join([
            " RUNSHARED=",
            "'",
            grepValue("Makefile", "RUNSHARED"),
            #' DYLD_LIBRARY_PATH=',
            #os.path.join(WORKDIR, 'libraries', 'usr', 'local', 'lib'),
            "'" ])

    # Look for environment value BUILDINSTALLER_BUILDPYTHON_MAKE_EXTRAS
    # and, if defined, append its value to the make command.  This allows
    # us to pass in version control tags, like GITTAG, to a build from a
    # tarball rather than from a vcs checkout, thus eliminating the need
    # to have a working copy of the vcs program on the build machine.
    #
    # A typical use might be:
    #      export BUILDINSTALLER_BUILDPYTHON_MAKE_EXTRAS=" \
    #                         GITVERSION='echo 123456789a' \
    #                         GITTAG='echo v3.6.0' \
    #                         GITBRANCH='echo 3.6'"

    make_extras = os.getenv("BUILDINSTALLER_BUILDPYTHON_MAKE_EXTRAS")
    if make_extras:
        make_cmd = "make " + make_extras + runshared_for_make
    else:
        make_cmd = "make" + runshared_for_make
    print("Running " + make_cmd)
    runCommand(make_cmd)

    make_cmd = "make altinstall"
        #shellQuote(rootDir),
        #runshared_for_make)
    print("Running " + make_cmd)
    runCommand(make_cmd)


def python_postbuild():
    curdir = os.getcwd()

    buildDir = os.path.join(WORKDIR, '_bld', 'python')
    embed_lib_dir = os.path.join(OUTDIR, 'lib')
    path_to_lib = os.path.join(embed_lib_dir, "python%s" % (getVersion()))
    print("Fix file modes")
    gid = grp.getgrnam('admin').gr_gid

    shared_lib_error = False
    for dirpath, dirnames, filenames in os.walk(OUTDIR):
        for dn in dirnames:
            os.chmod(os.path.join(dirpath, dn), STAT_0o775)
            os.chown(os.path.join(dirpath, dn), -1, gid)

        for fn in filenames:
            if os.path.islink(fn):
                continue

            # "chmod g+w $fn"
            p = os.path.join(dirpath, fn)
            st = os.stat(p)
            os.chmod(p, stat.S_IMODE(st.st_mode) | stat.S_IWGRP)
            os.chown(p, -1, gid)

            if fn in EXPECTED_SHARED_LIBS:
                # check to see that this file was linked with the
                # expected library path and version
                data = captureCommand("otool -L %s" % shellQuote(p))
                for sl in EXPECTED_SHARED_LIBS[fn]:
                    if ("\t%s " % sl) not in data:
                        print("Expected shared lib %s was not linked with %s"
                                % (sl, p))
                        shared_lib_error = True

    if shared_lib_error:
        fatal("Unexpected shared library errors.")

    if PYTHON_3:
        LDVERSION=None
        VERSION=None
        ABIFLAGS=None

        fp = open(os.path.join(buildDir, 'Makefile'), 'r')
        for ln in fp:
            if ln.startswith('VERSION='):
                VERSION=ln.split()[1]
            if ln.startswith('ABIFLAGS='):
                ABIFLAGS=ln.split()
                ABIFLAGS=ABIFLAGS[1] if len(ABIFLAGS) > 1 else ''
            if ln.startswith('LDVERSION='):
                LDVERSION=ln.split()[1]
        fp.close()

        LDVERSION = LDVERSION.replace('$(VERSION)', VERSION)
        LDVERSION = LDVERSION.replace('$(ABIFLAGS)', ABIFLAGS)
        config_suffix = '-' + LDVERSION
        if getVersionMajorMinor() >= (3, 6):
            config_suffix = config_suffix + '-darwin'
    else:
        config_suffix = ''      # Python 2.x

    # We added some directories to the search path during the configure
    # phase. Remove those because those directories won't be there on
    # the end-users system. Also remove the directories from _sysconfigdata.py
    # (added in 3.3) if it exists.

    include_path = '-I%s/libraries/usr/local/include' % (WORKDIR,)
    lib_path = '-L%s/libraries/usr/local/lib' % (WORKDIR,)

    # fix Makefile
    path = os.path.join(path_to_lib, 'config' + config_suffix, 'Makefile')
    fp = open(path, 'r')
    data = fp.read()
    fp.close()

    for p in (include_path, lib_path):
        data = data.replace(" " + p, '')
        data = data.replace(p + " ", '')

    fp = open(path, 'w')
    fp.write(data)
    fp.close()

    # fix _sysconfigdata
    #
    # TODO: make this more robust!  test_sysconfig_module of
    # distutils.tests.test_sysconfig.SysconfigTestCase tests that
    # the output from get_config_var in both sysconfig and
    # distutils.sysconfig is exactly the same for both CFLAGS and
    # LDFLAGS.  The fixing up is now complicated by the pretty
    # printing in _sysconfigdata.py.  Also, we are using the
    # pprint from the Python running the installer build which
    # may not cosmetically format the same as the pprint in the Python
    # being built (and which is used to originally generate
    # _sysconfigdata.py).

    import pprint
    if getVersionMajorMinor() >= (3, 6):
        # XXX this is extra-fragile
        path = os.path.join(path_to_lib,
            '_sysconfigdata_%s_darwin_darwin.py' % (ABIFLAGS,))
    else:
        path = os.path.join(path_to_lib, '_sysconfigdata.py')
    fp = open(path, 'r')
    data = fp.read()
    fp.close()
    # create build_time_vars dict
    if RUNNING_ON_PYTHON2:
        exec(data)
    else:
        g_dict = {}
        l_dict = {}
        exec(data, g_dict, l_dict)
        build_time_vars = l_dict['build_time_vars']
    vars = {}
    for k, v in build_time_vars.items():
        if type(v) == type(''):
            for p in (include_path, lib_path):
                v = v.replace(' ' + p, '')
                v = v.replace(p + ' ', '')
        vars[k] = v

    fp = open(path, 'w')
    # duplicated from sysconfig._generate_posix_vars()
    fp.write('# system configuration generated and used by'
                ' the sysconfig module\n')
    fp.write('build_time_vars = ')
    pprint.pprint(vars, stream=fp)
    fp.close()

    os.chdir(curdir)


def main():
    # First parse options and check if we can perform our work
    parseOptions()
    checkEnvironment()

    os.environ['MACOSX_DEPLOYMENT_TARGET'] = DEPTARGET
    os.environ['CC'] = CC
    os.environ['CXX'] = CXX

    if os.path.exists(WORKDIR):
        shutil.rmtree(WORKDIR)
    os.mkdir(WORKDIR)

    os.environ['LC_ALL'] = 'C'

    # Then build third-party libraries such as sleepycat DB4.
    buildLibraries()
    libraries_postbuild()
    # Now build python itself
    buildPython()
    python_postbuild()

    # And then build the documentation
    # Remove the Deployment Target from the shell
    # environment, it's no longer needed and
    # an unexpected build target can cause problems
    # when Sphinx and its dependencies need to
    # be (re-)installed.
    del os.environ['MACOSX_DEPLOYMENT_TARGET']


if __name__ == "__main__":
    main()
