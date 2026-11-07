"""Execute the reviewed SPC module-link patch against real static archives."""
import ctypes
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_build import REPO

# Pinned upstream method, used to demonstrate the missing final-link closure.
UPSTREAM_MAKE = '''    public function makeForUnix(array $env, PhpExtensionPackage $package, PackageBuilder $builder): void
    {
        $makeArgs = implode(' ', $package->re2cMakeVars());
        shell()->cd($package->getSourceRoot())
            ->setEnv($env)
            ->exec('make clean')
            ->exec("make -j{$builder->concurrency} {$makeArgs}")
            ->exec("make install {$makeArgs}")
            // distclean after install: phpize build residues (.lo/.libs/config.status) would
            // poison a later static in-tree build sharing this source dir (php-src's make
            // clean uses find, which does not follow the ext symlink)
            ->exec('make distclean');
    }'''

PHP_HARNESS = '''<?php
namespace StaticPHP\\Util {
    class SPCConfigUtil {
        public function __construct(private array $options = []) {}
        public function configForResolvedBuild(array $names, $installer): array {
            if ($names !== ['ext-fixture']) { throw new \\RuntimeException('Wrong closure root'); }
            return ['libs' => (($this->options['no_php'] ?? false) ? '' : '-lphp ') . '-ldep_a -ldep_b'];
        }
    }
}
namespace StaticPHP\\Package {
    use StaticPHP\\Util\\SPCConfigUtil;
    class PackageBuilder { public int $concurrency = 1; }
    class PhpExtensionPackage {
        public function __construct(private string $root) {}
        public function getSourceRoot(): string { return $this->root; }
        public function getName(): string { return 'ext-fixture'; }
        public function getExtensionName(): string { return 'fixture'; }
        public function getInstaller() { return null; }
        public function re2cMakeVars(): array { return []; }
        __METHOD__
    }
    class TestShell {
        private string $cwd;
        private array $env = [];
        public function cd(string $cwd): self { $this->cwd = $cwd; return $this; }
        public function setEnv(array $env): self { $this->env = $env; return $this; }
        public function exec(string $command): self {
            $process = proc_open($command, [0 => STDIN, 1 => STDOUT, 2 => STDERR], $pipes, $this->cwd, array_merge(getenv(), $this->env));
            $status = proc_close($process);
            if ($status !== 0) { throw new \\RuntimeException("Command failed: {$command}"); }
            return $this;
        }
    }
    function shell(): TestShell { return new TestShell(); }
    function clean_spaces(string $text): string { return trim(preg_replace('/\\s+/', ' ', $text)); }
    $package = new PhpExtensionPackage($argv[1]);
    $package->makeForUnix(['LIBS' => '-ldep_a -ldep_b'], $package, new PackageBuilder());
}
'''


class SharedLinkContracts(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(tool) for tool in ('php', 'cc', 'ar', 'make', 'nm')), 'native fixture tools are unavailable')
    def test_final_phpize_link_contains_transitive_static_closure_without_libphp(self):
        registry = json.loads((REPO / 'runtimes/php/config/spc-source-patches.json').read_text())
        patches = [row for row in registry['patches'] if row['file'].endswith('/PhpExtensionPackage.php')]
        method = patches[0]['replace']['new'] if patches else UPSTREAM_MAKE
        with tempfile.TemporaryDirectory(prefix='shared-link-') as tmp:
            root = Path(tmp)
            tools, source = root / 'tools', root / 'source'
            tools.mkdir()
            source.mkdir()
            # dep_a -> dep_b -> a second dep_a object requires archive rescanning.
            for name, text in {
                'module': 'extern int dep_entry(void); int fixture(void) { return dep_entry(); }',
                'entry': 'extern int dep_leaf(void); int dep_entry(void) { return dep_leaf(); }',
                'leaf': 'extern int dep_tail(void); int dep_leaf(void) { return dep_tail(); }',
                'tail': 'int dep_tail(void) { return 42; }',
            }.items():
                path = tools / f'{name}.c'
                path.write_text(text + '\n')
                subprocess.run(['cc', '-fPIC', '-c', str(path), '-o', str(source / f'{name}.o')], check=True, capture_output=True)
            for name, objects in [('dep_a', ['entry', 'tail']), ('dep_b', ['leaf'])]:
                subprocess.run(['ar', 'rcs', str(source / f'lib{name}.a'), *[str(source / f'{obj}.o') for obj in objects]], check=True, capture_output=True)
            # phpize's module recipe does not reference LIBS, only *_SHARED_LIBADD.
            (source / 'Makefile').write_text('FIXTURE_SHARED_LIBADD = -ldep_a\nall: fixture.so\nfixture.so: module.o\n\tcc -shared -o $@ module.o -L. $(FIXTURE_SHARED_LIBADD)\nclean:\n\trm -f fixture.so\ninstall:\n\t@true\ndistclean:\n\t@true\n')
            harness = tools / 'link.php'
            harness.write_text(PHP_HARNESS.replace('__METHOD__', method))
            result = subprocess.run(['php', '-n', str(harness), str(source)], cwd=tools, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            undefined = subprocess.check_output(['nm', '-u', str(source / 'fixture.so')], text=True)
            self.assertNotIn('dep_leaf', undefined, 'Final module link discarded the transitive dependency closure')
            self.assertNotIn('dep_tail', undefined, 'Static archives were not grouped for rescanning')
            library = ctypes.CDLL(str(source / 'fixture.so'))
            self.assertEqual(library.fixture(), 42)


if __name__ == '__main__':
    unittest.main()
