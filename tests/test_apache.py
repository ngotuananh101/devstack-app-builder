"""Apache runtime contracts: pins, source extraction, staging and packaging.

These tests do not build httpd. They exercise the parts that decide what a
bundle contains and how it is named, which is where a silent mistake costs a
multi-hour rebuild: the dependency pins, the way an archive is unpacked, the
launcher that pins ServerRoot, and the archive identity published to a release.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "runtimes/apache/scripts"
CONFIG = REPO / "runtimes/apache/config"


def load(name):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location("apache_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def module_config():
    return json.loads((CONFIG / "modules.json").read_text())


class PinContracts(unittest.TestCase):
    def setUp(self):
        self.source = load("source")

    def test_plan_pins_one_musl_dynamic_source_and_every_dependency(self):
        plan = self.source.plan([])
        self.assertEqual(plan["runtime"], "apache")
        self.assertEqual(plan["target"], "musl-dynamic")
        self.assertEqual(plan["architecture"], "x86_64")
        self.assertNotIn("profile", plan)
        names = [entry["name"] for entry in plan["dependencies"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("musl", names)
        self.assertEqual({entry["role"] for entry in plan["dependencies"]},
                         {"loader", "link", "dbd", "core"})

    def test_the_libc_target_cannot_be_selected(self):
        for value in ("glibc", "musl", "static"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source.plan([value])

    def test_an_unpinned_version_is_refused(self):
        with self.assertRaises(ValueError):
            self.source.plan(["2.4.99"])

    def test_dependency_pins_are_https_with_full_digests(self):
        for entry in self.source.load_dependencies():
            with self.subTest(name=entry["name"]):
                self.assertTrue(entry["url"].startswith("https://"))
                self.assertRegex(entry["sha256"], r"^[a-f0-9]{64}$")
                self.assertTrue(entry["license"])

    def test_every_dso_is_named_once_and_never_static(self):
        data = module_config()
        self.assertEqual(len(data["dso"]), len(set(data["dso"])))
        self.assertFalse(set(data["dso"]) & set(data["static"]))
        self.assertFalse({row["name"] for row in data["excluded"]} & set(data["dso"]))
        for required in ("ssl", "http2", "md", "brotli", "lua", "dav", "proxy_fcgi", "rewrite"):
            self.assertIn(required, data["dso"])

    def test_the_default_mpm_is_one_of_the_shared_mpms(self):
        data = module_config()
        self.assertIn(data["mpm"]["default"], data["mpm"]["shared"])


class ExtractionContracts(unittest.TestCase):
    def setUp(self):
        self.fetch = load("fetch")

    def archive(self, members):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            for name, payload, mtime in members:
                info = tarfile.TarInfo(name)
                info.mtime = mtime
                if payload is None:
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                else:
                    info.size = len(payload)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(payload))
        return stream.getvalue()

    def write(self, directory, data):
        path = Path(directory) / "source.tar.gz"
        path.write_bytes(data)
        return path

    def test_download_retries_a_transient_gateway_error(self):
        # GitHub's release CDN answers 504 for a single asset while the rest of
        # the same release serves normally, and recovers within seconds. Without
        # a retry one such answer kills a build that has already spent an hour
        # compiling.
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "pcre2.tar.gz"
            attempts = []

            def fake_run(command, **kwargs):
                attempts.append(command)
                if len(attempts) == 1:
                    return subprocess.CompletedProcess(command, 22, "",
                                                      "curl: (22) 504")
                Path(command[command.index("--output") + 1]).write_bytes(b"archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(self.fetch.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(self.fetch.time, "sleep"):
                self.fetch.download("https://example.invalid/pcre2.tar.gz", destination)
            self.assertEqual(destination.read_bytes(), b"archive")
            self.assertEqual(len(attempts), 2)
            for command in attempts:
                self.assertIn("=https", command)

    def test_download_gives_up_and_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "pcre2.tar.gz"
            attempts = []

            def fake_run(command, **kwargs):
                attempts.append(command)
                return subprocess.CompletedProcess(command, 22, "", "curl: (22) 504")

            with mock.patch.object(self.fetch.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(self.fetch.time, "sleep"):
                with self.assertRaisesRegex(
                        ValueError, "Cannot download.*after %d attempts"
                        % self.fetch.FETCH_ATTEMPTS):
                    self.fetch.download("https://example.invalid/pcre2.tar.gz", destination)
            self.assertGreater(len(attempts), 1)
            self.assertFalse(destination.exists())

    def test_a_digest_mismatch_says_what_arrived_instead(self):
        # A bare "expected X, got Y" is not diagnosable: it cannot distinguish a
        # changed upstream archive from a proxy that answered 200 with an HTML
        # error page, and that difference decides whether the pin or the route
        # is at fault. The message must carry the size and the content type.
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "downloads").mkdir()
            payload = b"<html>Service Unavailable</html>"

            def fake_run(command, **kwargs):
                Path(command[command.index("--output") + 1]).write_bytes(payload)
                return subprocess.CompletedProcess(command, 0, "", "")

            entry = {"name": "pcre2", "url": "https://example.invalid/pcre2-10.49.tar.gz",
                     "sha256": "0" * 64}
            with mock.patch.object(self.fetch.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(self.fetch, "content_type",
                                      return_value="text/html"):
                with self.assertRaises(ValueError) as caught:
                    self.fetch.fetch_one(entry, work)
            message = str(caught.exception)
            self.assertIn(hashlib.sha256(payload).hexdigest(), message)
            self.assertIn(f"{len(payload)} bytes", message)
            self.assertIn("text/html", message)
            # Nothing was extracted from an archive that failed its pin.
            self.assertFalse((work / "sources" / "pcre2").exists())

    def test_a_github_failure_falls_back_to_the_mirror(self):
        # GitHub's release surface has answered 504 for hours at a time while the
        # rest of the internet served normally. The direct attempts are still
        # made first and in full: the mirror is a last resort, not the normal path.
        url = ("https://github.com/nghttp2/nghttp2/releases/download/"
               "v1.71.0/nghttp2-1.71.0.tar.gz")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "nghttp2.tar.gz"
            attempts = []

            def fake_run(command, **kwargs):
                attempts.append(command)
                if len(attempts) <= self.fetch.FETCH_ATTEMPTS:
                    return subprocess.CompletedProcess(command, 22, "", "curl: (22) 504")
                Path(command[command.index("--output") + 1]).write_bytes(b"archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(self.fetch.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(self.fetch.time, "sleep"):
                self.fetch.download(url, destination)
            self.assertEqual(destination.read_bytes(), b"archive")
            direct = [c[-1] for c in attempts[:-1]]
            self.assertEqual(len(direct), self.fetch.FETCH_ATTEMPTS)
            for attempted in direct:
                self.assertEqual(attempted, url)
            self.assertEqual(attempts[-1][-1], self.fetch.MIRROR + url)

    def test_the_mirror_is_not_tried_for_a_host_that_is_not_github(self):
        # Only GitHub's release surface fails this way. Proxying zlib.net would
        # turn a clear upstream answer into somebody else's outage.
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "zlib.tar.gz"
            attempts = []

            def fake_run(command, **kwargs):
                attempts.append(command)
                return subprocess.CompletedProcess(command, 22, "", "curl: (22) 504")

            with mock.patch.object(self.fetch.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(self.fetch.time, "sleep"):
                with self.assertRaisesRegex(ValueError, "Cannot download"):
                    self.fetch.download("https://zlib.net/zlib-1.4.tar.gz", destination)
            self.assertEqual(len(attempts), self.fetch.FETCH_ATTEMPTS)
            for command in attempts:
                self.assertNotIn("gh-proxy.org", command[-1])

    def test_a_mirror_that_returns_different_bytes_is_refused(self):
        # The pin is the whole reason a third-party mirror is safe to use: a mirror
        # that rewrites, truncates or serves an error page must fail exactly like
        # an upstream that changed the archive.
        url = ("https://github.com/pcre2project/pcre2/releases/download/"
               "pcre2-10.49/pcre2-10.49.tar.gz")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "downloads").mkdir()
            attempts = []

            def fake_run(command, **kwargs):
                attempts.append(command)
                if len(attempts) <= self.fetch.FETCH_ATTEMPTS:
                    return subprocess.CompletedProcess(command, 22, "", "curl: (22) 504")
                Path(command[command.index("--output") + 1]).write_bytes(b"<html>nope</html>")
                return subprocess.CompletedProcess(command, 0, "", "")

            entry = {"name": "pcre2", "url": url, "sha256": "0" * 64}
            with mock.patch.object(self.fetch.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(self.fetch.time, "sleep"), \
                    mock.patch.object(self.fetch, "content_type", return_value="text/html"):
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    self.fetch.fetch_one(entry, work)
            self.assertEqual(attempts[-1][-1], self.fetch.MIRROR + url)
            # Nothing survives an archive that failed its pin.
            self.assertFalse((work / "downloads" / "pcre2.tar.gz").exists())
            self.assertFalse((work / "sources" / "pcre2").exists())

    def test_member_times_survive_extraction(self):
        # A generated file that looks newer than its source stops make from
        # trying to regenerate it with the packaged `missing` script, which fails
        # on any autotools version mismatch between the archive and the host.
        data = self.archive([("src", None, 1_000_000),
                             ("src/configure.ac", b"input\n", 1_100_000),
                             ("src/aclocal.m4", b"generated\n", 1_200_000)])
        with tempfile.TemporaryDirectory() as temporary:
            archive = self.write(temporary, data)
            destination = Path(temporary) / "extracted"
            root = self.fetch.extract(archive, destination)
            self.assertEqual(root, destination / "src")
            self.assertGreater((root / "aclocal.m4").stat().st_mtime,
                               (root / "configure.ac").stat().st_mtime)
            self.assertEqual((root / "aclocal.m4").read_text(), "generated\n")

    def test_links_and_special_files_are_refused(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            link = tarfile.TarInfo("src/escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        with tempfile.TemporaryDirectory() as temporary:
            archive = self.write(temporary, stream.getvalue())
            with self.assertRaises(ValueError):
                self.fetch.extract(archive, Path(temporary) / "out")

    def test_two_roots_are_refused(self):
        data = self.archive([("one/a", b"a\n", 0), ("two/b", b"b\n", 0)])
        with tempfile.TemporaryDirectory() as temporary:
            archive = self.write(temporary, data)
            with self.assertRaises(ValueError):
                self.fetch.extract(archive, Path(temporary) / "out")


class ToolchainContracts(unittest.TestCase):
    """The bundle must not take its musl from the build host's packages.

    Ubuntu 24.04 ships musl 1.2.4 while the pin is 1.2.5, so a host musl-gcc is a
    host dependency the bundle cannot have: the loader and the objects linked
    against it must come from the same pinned release, and that release is compiled
    here. These tests pin the mechanics that make that true.
    """

    def setUp(self):
        self.loader = load("loader")

    def test_the_toolchain_is_built_from_the_pinned_source(self):
        # musl derives musl-gcc and its specs from --prefix, and the install runs
        # through DESTDIR so it never writes /lib on the build host.
        text = (SCRIPTS / "loader.py").read_text()
        self.assertIn('"--prefix=" + str(toolchain)', text)
        self.assertIn('"--syslibdir=" + str(toolchain / PurePosixPath(TOOLCHAIN_LOADER).parent)',
                      text)
        self.assertIn('"DESTDIR=" + str(stage)', text)
        # The interpreter the compiler names must be the toolchain's own loader,
        # not /lib: configure runs its test programs on hosts that have no musl.
        self.assertEqual(self.loader.TOOLCHAIN_LOADER, "lib/ld-musl-x86_64.so.1")
        # ...and the portable /lib name lives in stage.py, the only step that has
        # a shipped bundle to write it back into.
        self.assertNotIn("SYSLOADER", text)

    def test_no_step_compiles_with_the_hosts_musl_gcc(self):
        # A default of musl-gcc would silently reintroduce the dependency this
        # change removes, in any one of the three steps.
        build = (SCRIPTS / "build.sh").read_text()
        self.assertIn('compiler="${APACHE_CC:-cc}"', build)
        self.assertIn('--cc "$cc"', build)
        deps = (SCRIPTS / "deps.py").read_text()
        httpd = (SCRIPTS / "httpd.py").read_text()
        for text in (deps, httpd):
            self.assertNotIn('"musl-gcc"', text)
            self.assertIn('"--cc"', text)
        # deps.py may not fall back to a module-level constant either.
        self.assertNotIn('CC = "musl-gcc"', deps)

    def test_the_probe_compiles_and_runs_with_the_built_toolchain(self):
        # A probe that only asks the compiler where its loader is reports the
        # host's musl, which is the dependency being removed. What the toolchain
        # produces has to be executed instead.
        text = (SCRIPTS / "loader.py").read_text()
        self.assertIn('toolchain["libc"], str(program)', text)
        self.assertNotIn('"-print-file-name="', text)
        self.assertFalse(hasattr(self.loader, "toolchain_loader"))

    def test_the_kernel_uapi_roots_come_from_the_compiler(self):
        # The second CI failure: musl ships no kernel UAPI headers, and the build
        # assumed they were all under /usr/include. Fedora has both there; Ubuntu
        # splits them, so <linux/mman.h> resolved and its <asm/mman.h> did not.
        # The roots are read from the compiler's own search list instead.
        text = (SCRIPTS / "loader.py").read_text()
        self.assertIn("uapi.discover(", text)
        self.assertNotIn("-idirafter /usr/include", text)
        for script in ("deps.py", "httpd.py"):
            self.assertNotIn("-idirafter /usr/include", (SCRIPTS / script).read_text())
            self.assertIn("uapi.", (SCRIPTS / script).read_text())
        # The probe includes a kernel header, so a root that cannot resolve it
        # fails before the dependency build rather than inside OpenSSL.
        probe = (SCRIPTS / "loader.py").read_text().split("def probe_toolchain", 1)[1]
        self.assertIn("#include <linux/mman.h>", probe)

    def test_uapi_accepts_split_roots_and_refuses_a_partial_one(self):
        # Linux/ and asm/ in different directories is the Ubuntu layout; a single
        # directory holding only linux/ cannot satisfy <linux/mman.h>.
        uapi = load("uapi")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            kernel = base / "include"
            multiarch = base / "x86_64-linux-gnu"
            (kernel / "linux").mkdir(parents=True)
            (multiarch / "asm").mkdir(parents=True)
            split = "%s%s%s" % (kernel, uapi.SEPARATOR, multiarch)
            # Separate words, not one string: a caller putting the whole string in
            # argv would pass "-idirafter /usr/include" as a single argument.
            self.assertEqual(uapi.flags(split),
                             ["-idirafter", str(kernel), "-idirafter", str(multiarch)])
            self.assertEqual(uapi.env_flags(split),
                             "-idirafter %s -idirafter %s" % (kernel, multiarch))
            with self.assertRaisesRegex(ValueError, "asm/"):
                uapi.flags(str(kernel))
            with self.assertRaisesRegex(ValueError, "absolute"):
                uapi.flags("relative/include")
            # A root that does not exist is refused rather than silently ignored.
            with self.assertRaisesRegex(ValueError, "No such kernel include root"):
                uapi.flags(split + uapi.SEPARATOR + str(base / "absent"))

    def test_discovery_reads_the_compilers_own_search_list(self):
        # The Ubuntu 24.04 layout, replayed through a fake compiler: linux/ in one
        # directory and asm/ in the multiarch one. Both must be found, in the
        # order the compiler searches them, and nothing outside that list counts.
        uapi = load("uapi")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            kernel = base / "usr/include"
            multiarch = base / "usr/include/x86_64-linux-gnu"
            (kernel / "linux").mkdir(parents=True)
            (multiarch / "asm").mkdir(parents=True)
            gcc_private = base / "lib/gcc/include"
            gcc_private.mkdir(parents=True)
            compiler = base / "fake-cc"
            compiler.write_text(
                "#!/bin/sh\n"
                "cat >&2 <<'EOF'\n"
                "ignoring nonexistent directory \"%s\"\n"
                "#include \"...\" search starts here:\n"
                "#include <...> search starts here:\n"
                " %s\n %s\n %s\n"
                "End of search list.\n"
                "EOF\n" % (base / "absent", gcc_private, kernel, multiarch))
            compiler.chmod(0o755)
            self.assertEqual(uapi.discover(compiler), [str(kernel), str(multiarch)])
            # The same compiler with only the first directory in its list is
            # refused: <linux/mman.h> would resolve and <asm/mman.h> would not.
            del_listed = compiler.with_name("fake-cc-one-root")
            del_listed.write_text(
                "#!/bin/sh\n"
                "cat >&2 <<'EOF'\n"
                "#include <...> search starts here:\n"
                " %s\n"
                "End of search list.\n"
                "EOF\n" % kernel)
            del_listed.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "asm/"):
                uapi.discover(del_listed)
            # A compiler that does not report a list at all is refused too.
            mute = compiler.with_name("fake-cc-mute")
            mute.write_text("#!/bin/sh\nexit 0\n")
            mute.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "include search list"):
                uapi.discover(mute)

    def test_the_interpreter_is_rewritten_for_the_bundle(self):
        # The build's compiler names the toolchain loader so configure can run its
        # test programs on a host with no musl; a shipped ELF must not carry that
        # workspace path, and the rewrite is length-preserving.
        elf = load("elf")
        sysloader = "/lib/ld-musl-x86_64.so.1"
        with tempfile.TemporaryDirectory() as temporary:
            program = Path(temporary) / "prog"
            build = subprocess.run(
                ["cc", "-O2", "-Wl,-dynamic-linker," + sysloader,
                 "-o", str(program), "-xc", "-"],
                input="int main(void){return 0;}\n", text=True, capture_output=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            self.assertEqual(elf.interpreter(program), sysloader)
            # A path that cannot fit is refused rather than written over the
            # following bytes; this is a segment-layout problem, not a patch.
            with self.assertRaisesRegex(ValueError, "cannot fit"):
                elf.set_interpreter(program, "/" + "x" * 200)
            self.assertEqual(elf.interpreter(program), sysloader)
            longer = temporary + "/" + "l" * 8 + "/loader"
            Path(longer).parent.mkdir(parents=True, exist_ok=True)
            if len(longer) == len(sysloader):
                self.assertFalse(elf.set_interpreter(program, longer))
                self.assertEqual(elf.interpreter(program), longer)

    def test_staging_makes_every_program_name_the_portable_loader(self):
        # Every staged program goes through portable_interpreter, or a build
        # workspace path would ship inside the bundle and fail elsewhere.
        text = (SCRIPTS / "stage.py").read_text()
        staging = text.split("def stage_binaries", 1)[1].split("def stage_modules", 1)[0]
        # Both copy sites in stage_binaries rewrite it: httpd's own programs and
        # the dependency-prefix programs (openssl).
        self.assertEqual(staging.count("portable_interpreter("), 2)
        self.assertIn('SYSLOADER = "/lib/" + LOADER_NAME', text)
        stage = load("stage")
        self.assertEqual(stage.SYSLOADER, "/lib/ld-musl-x86_64.so.1")

    def test_the_wrapper_names_the_host_compiler_that_built_it(self):
        # musl's own wrapper execs a bare `gcc`; a host that only provides `cc`
        # would then fail on every dependency. REALGCC stays overridable.
        with tempfile.TemporaryDirectory() as temporary:
            toolchain = Path(temporary) / "toolchain"
            (toolchain / "bin").mkdir(parents=True)
            (toolchain / "lib").mkdir()
            (toolchain / "lib/musl-gcc.specs").write_text("*link:\n")
            real = self.loader.write_wrapper(toolchain, "cc")
            text = (toolchain / "bin/musl-gcc").read_text()
            self.assertEqual(real, shutil.which("cc"))
            self.assertIn('${REALGCC:-%s}' % real, text)
            self.assertIn(str(toolchain / "lib/musl-gcc.specs"), text)
            self.assertTrue(os.access(toolchain / "bin/musl-gcc", os.X_OK))
            # The host compiler named in the wrapper must not be musl-gcc itself.
            self.assertNotIn("musl-gcc}", text)

    def test_a_loader_without_the_pinned_banner_is_refused(self):
        # inspect() trusts the banner, so it is the banner that must refuse a
        # binaries that is not the release the plan pinned.
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "libc.so"
            fake.write_text("#!/bin/sh\necho 'Version 9.9.9' >&2\n")
            fake.chmod(0o755)
            self.assertEqual(self.loader.banner_version(fake), "9.9.9")
            silent = Path(temporary) / "silent"
            silent.write_text("#!/bin/sh\nexit 0\n")
            silent.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "Not a musl loader"):
                self.loader.banner_version(silent)


class LauncherContracts(unittest.TestCase):
    def setUp(self):
        self.packaging = REPO / "runtimes/apache/packaging"

    def test_httpd_launcher_pins_the_bundles_own_server_root(self):
        # httpd bakes HTTPD_ROOT at configure time. Without -d the bundle reads the
        # build host's conf/httpd.conf and looks healthy while ignoring its own.
        stage = load("stage")
        self.assertEqual(stage.SERVER_ROOT_ARGUMENT["httpd"], '-d "$ROOT"')
        template = (self.packaging / "launcher").read_text()
        self.assertEqual(template.count("@NAME@"), 2)
        self.assertEqual(template.count("@EXTRA@"), 1)
        self.assertIn('--library-path "$LD_LIBRARY_PATH"', template)

    def test_launch_script_exports_everything_the_runtime_needs(self):
        script = (self.packaging / "launch.sh").read_text()
        self.assertIn('LD_LIBRARY_PATH="$ROOT/lib/musl:$ROOT/lib"', script)
        self.assertIn('APACHE_SERVER_ROOT="$ROOT"', script)
        self.assertIn("unset LD_PRELOAD LD_AUDIT", script)
        self.assertIn('OPENSSL_CONF="$ROOT/ssl/openssl.cnf"', script)

    def test_apachectl_is_a_wrapper_not_a_generated_script(self):
        script = (self.packaging / "apachectl").read_text()
        self.assertIn('ROOT=$(CDPATH=', script)
        self.assertIn('exec "$ROOT/bin/httpd"', script)
        self.assertNotIn("/tmp/abuild", script)

    def test_staged_program_sets_name_what_they_drop(self):
        stage = load("stage")
        self.assertIn("apachectl", stage.DROPPED)
        self.assertIn("suexec", stage.UNLAUNCHED)
        self.assertEqual(stage.EXTRA_PROGRAMS, ("openssl",))

    def test_everything_in_packaging_is_a_shell_script(self):
        # The CI contract step runs `sh -n` over every file in packaging/, so a
        # configuration template there would fail the whole job. httpd.conf is
        # therefore a config/ template, and this pins that split.
        stage = load("stage")
        self.assertTrue((stage.CONFIG / "httpd.conf").is_file())
        for path in sorted(self.packaging.iterdir()):
            with self.subTest(path=path.name):
                self.assertNotEqual(path.suffix, ".conf")
                result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


class PackageContracts(unittest.TestCase):
    def setUp(self):
        self.package = load("package")
        self.publish = load_publish()

    def bundle(self, root, verification="PASSED"):
        parts = {"schema_version": 1, "runtime": "apache", "apache": {"version": "2.4.70"},
                 "target": "musl-dynamic", "architecture": "x86_64", "linkage": "dynamic",
                 "build_id": "fixture", "verification": verification,
                 "builder": {"commit": "a" * 40},
                 "loader": {"path": "lib/musl/ld-musl-x86_64.so.1", "sha256": "0" * 64},
                 "modules": {"dso": ["ssl", "http2", "md", "brotli", "lua", "dav",
                                     "proxy_fcgi", "rewrite"]},
                 "mpm": {"default": "event", "shared": ["event", "worker", "prefork"]}}
        for relative in self.package.REQUIRED:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        for relative in ("bin/httpd", "bin/apachectl", "lib/musl/ld-musl-x86_64.so.1",
                         "modules/mod_ssl.so"):
            (root / relative).chmod(0o755)
        (root / "manifest.json").write_text(json.dumps(parts))
        return parts

    def test_identity_is_version_target_arch_and_build_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root / "bundle")
            archive = self.package.package(root / "bundle", root / "assets")
            self.assertEqual(archive.name, "apache-2.4.70-musl-dynamic-x86_64-fixture.tar.gz")
            self.assertTrue(archive.with_name(archive.name + ".sha256").is_file())

    def test_unverified_or_incomplete_bundles_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root / "unverified", verification="NOT_RUN")
            with self.assertRaisesRegex(ValueError, "verification"):
                self.package.package(root / "unverified", root / "assets")
            self.bundle(root / "incomplete")
            (root / "incomplete/modules/mod_lua.so").unlink()
            with self.assertRaisesRegex(ValueError, "incomplete"):
                self.package.package(root / "incomplete", root / "assets")

    def test_a_published_name_is_never_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root / "bundle")
            self.package.package(root / "bundle", root / "assets")
            with self.assertRaises(FileExistsError):
                self.package.package(root / "bundle", root / "assets")

    def test_publication_accepts_the_archive_and_every_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root / "bundle")
            # bin/openssl is a launcher over libexec/bin/openssl in the real bundle.
            for relative in ("bin/openssl", "libexec/bin/httpd"):
                path = root / "bundle" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            for relative in ("conf/httpd.conf", "conf/original/httpd.conf",
                             "ssl/openssl.cnf", "modules/mod_md.so", "modules/mod_dav.so",
                             "modules/mod_proxy_fcgi.so", "modules/mod_rewrite.so",
                             "lib/apr-util-1/apr_dbd_sqlite3-1.so"):
                path = root / "bundle" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            assets = root / "assets"
            self.package.package(root / "bundle", assets)
            self.assertEqual(len(self.publish.validate_assets(assets)), 3)

    def test_publication_accepts_relative_soname_links_and_refuses_escaping_ones(self):
        # stage_libraries keeps soname aliases by name, and brotli chains two of
        # them (libbrotlicommon.so -> .so.1 -> .so.1.2.1). The publisher must
        # follow the chain to a real file rather than trust the first hop.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root / "bundle")
            extras = ("bin/openssl", "libexec/bin/httpd", "conf/httpd.conf",
                      "conf/original/httpd.conf", "ssl/openssl.cnf",
                      "modules/mod_md.so", "modules/mod_dav.so",
                      "modules/mod_proxy_fcgi.so", "modules/mod_rewrite.so",
                      "lib/apr-util-1/apr_dbd_sqlite3-1.so")
            for relative in extras:
                path = root / "bundle" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                if relative in self.publish.APACHE_PROGRAMS:
                    path.chmod(0o755)
            library = root / "bundle/lib/libz.so.1.3.2"
            library.write_bytes(b"fixture")
            (root / "bundle/lib/libz.so.1").symlink_to("libz.so.1.3.2")
            (root / "bundle/lib/libz.so").symlink_to("libz.so.1")
            assets = root / "assets"
            self.package.package(root / "bundle", assets)
            self.assertEqual(len(self.publish.validate_assets(assets)), 3)
            # A link that leaves the bundle root is refused even though the member
            # path itself is contained: tar would write through it on extraction.
            for relative in ("libz.so", "libz.so.1"):
                (root / "bundle/lib" / relative).unlink()
            (root / "bundle/lib/libz.so.1").symlink_to("../../outside")
            (root / "bundle/lib/libz.so").symlink_to("libz.so.1")
            self.package.package(root / "bundle", root / "bad-assets")
            with self.assertRaisesRegex(ValueError, "Unsafe archive layout"):
                self.publish.validate_assets(root / "bad-assets")

    def test_publication_rejects_a_bundle_without_its_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root / "bundle")
            for relative in ("bin/openssl", "libexec/bin/httpd", "conf/httpd.conf",
                             "conf/original/httpd.conf", "ssl/openssl.cnf",
                             "modules/mod_md.so", "modules/mod_dav.so",
                             "modules/mod_proxy_fcgi.so", "modules/mod_rewrite.so",
                             "lib/apr-util-1/apr_dbd_sqlite3-1.so"):
                path = root / "bundle" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            assets = root / "assets"
            self.package.package(root / "bundle", assets)
            # The packager's own completeness check is the first gate; this also
            # proves the publisher independently refuses an archive that lost it.
            stage = load_stage_required()
            self.assertIn("lib/musl/ld-musl-x86_64.so.1", stage)
            name = next(assets.glob("*.tar.gz"))
            rebuilt = root / "rebuilt.tar.gz"
            with tarfile.open(name) as source, tarfile.open(rebuilt, "w:gz") as output:
                for member in source.getmembers():
                    if member.name.endswith("lib/musl/ld-musl-x86_64.so.1"):
                        continue
                    output.addfile(member, source.extractfile(member) if member.isfile() else None)
            name.write_bytes(rebuilt.read_bytes())
            # Re-sign the tampered archive so the loader check, not the checksum,
            # is what refuses it: the two gates are independent on purpose.
            with name.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            name.with_name(name.name + ".sha256").write_text(digest + "  " + name.name + "\n")
            with self.assertRaisesRegex(ValueError, "loader"):
                self.publish.validate_assets(assets)


def load_publish():
    spec = importlib.util.spec_from_file_location(
        "php_publish_for_apache", REPO / "runtimes/php/scripts/publish.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_stage_required():
    return load("package").REQUIRED


class ManifestContracts(unittest.TestCase):
    def test_the_builder_commit_is_recorded_not_guessed(self):
        stage = load("stage")
        record = stage.builder_record()
        self.assertRegex(record["commit"], r"^[a-f0-9]{40}$")
        self.assertRegex(record["source_sha256"], r"^[a-f0-9]{64}$")
        # The digest must move when the builder changes.
        self.assertNotEqual(record["source_sha256"], hashlib.sha256(b"").hexdigest())


class PlanningContracts(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "runtime_updates_for_apache", REPO / "scripts/updates.py")
        self.planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.planner)

    def test_apache_rows_carry_the_pinned_identity(self):
        with mock.patch.object(self.planner, "published_apache", return_value={}):
            rows = self.planner.apache_matrix("owner/repo", bypass=False)["include"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["target"], "musl-dynamic")
            self.assertEqual(row["architecture"], "x86_64")
            self.assertNotIn("profile", row)
            self.assertRegex(row["apache_version"], r"^2\.4\.[0-9]+$")

    def test_a_published_apache_bundle_removes_its_row(self):
        with mock.patch.object(self.planner, "published_apache", return_value={}):
            rows = self.planner.apache_matrix("owner/repo", bypass=False)["include"]
        version = rows[0]["apache_version"]
        published = {(version, "musl-dynamic", "x86_64"): version}
        with mock.patch.object(self.planner, "published_apache", return_value=published):
            self.assertEqual(self.planner.apache_matrix("owner/repo", bypass=False)["include"],
                             [row for row in rows if row["apache_version"] != version])
            # A bypass ignores published history on purpose.
            self.assertEqual(len(self.planner.apache_matrix("owner/repo", bypass=True)["include"]),
                             len(rows))

    def test_public_history_ignores_other_architectures_and_retired_targets(self):
        root = "apache-2.4.70-musl-dynamic-x86_64-fixture"
        assets = [{"name": root + suffix, "id": index + 10, "state": "uploaded", "size": 100}
                  for index, suffix in enumerate((".tar.gz", ".tar.gz.sha256", ".manifest.json"))]
        assets += [{"name": "apache-2.4.70-musl-dynamic-aarch64-fixture.tar.gz",
                    "id": 90, "state": "uploaded", "size": 100}]
        manifest = {"schema_version": 1, "runtime": "apache", "apache": {"version": "2.4.70"},
                    "target": "musl-dynamic", "architecture": "x86_64",
                    "build_id": "fixture", "verification": "PASSED"}
        releases = [{"tag_name": "runtimes-example", "id": 1, "draft": False, "prerelease": False}]

        def pages(endpoint):
            return iter(releases if endpoint.endswith("/releases") else assets)

        with mock.patch.object(self.planner.php, "pages", side_effect=pages), \
                mock.patch.object(self.planner.php, "gh_json", return_value=manifest):
            self.assertEqual(self.planner.published_apache("owner/repo"),
                             {("2.4.70", "musl-dynamic", "x86_64"): "2.4.70"})


if __name__ == "__main__":
    unittest.main()
