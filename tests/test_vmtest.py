"""Initramfs packaging tests; set VMTEST_KERNEL for an additional real VM boot."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest


VMTEST = Path(__file__).resolve().parents[1] / "vmtest"
TOOLS = ("gcc", "cpio", "gzip", "ldd", "readelf", "file")


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "requires vmtest build tools")
class VMTestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vmtest-test.")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        qemu = self.bin / "qemu-system-x86_64"
        qemu.write_text("#!/bin/sh\necho 'RESULT PASS'\n")
        qemu.chmod(0o755)
        self.kernel = self.directory / "bzImage"
        self.kernel.touch()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.env.pop("PYTHONHOME", None)
        self.env.pop("PYTHONPATH", None)
        self.env["PYTHONNOUSERSITE"] = "1"
        self.env["TMPDIR"] = str(self.directory)

    def run_vmtest(self, source, *options, success=True, live=False):
        env = self.env.copy()
        kernel = self.kernel
        if live:
            env["PATH"] = os.environ["PATH"]
            kernel = os.environ["VMTEST_KERNEL"]
        result = subprocess.run(
            [str(VMTEST), "--kern", str(kernel), "--out", str(self.directory / "console.log"),
             "--keep", "--timeout", "30", *map(str, options), str(source)],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        match = re.search(r"build directory kept: (.+)", result.stdout)
        root = Path(match[1]) / "root" if match else None
        return result, root

    def make_python_fixture(self):
        # Spaces exercise shebang relocation and paths passed through the helper.
        project = self.directory / "python project"
        project.mkdir()
        package = project / "localpkg"
        package.mkdir()
        (package / "__init__.py").write_text("from .values import answer\n")
        (package / "values.py").write_text("answer = 42\n")
        namespace = project / "namespacepkg"
        namespace.mkdir()
        (namespace / "child.py").write_text("value = 17\n")
        resources = project / "namespace_data"
        resources.mkdir()
        (resources / "data.txt").write_text("namespace data")
        site = self.directory / "site packages"
        site.mkdir()
        installed = site / "installedpkg"
        installed.mkdir()
        (installed / "__init__.py").write_text(
            "import os\nfrom pathlib import Path\n"
            f"if os.getpid() != 1: Path({str(self.directory / 'imported-on-host')!r}).touch()\n"
            "value = Path(__file__).with_name('data.txt').read_text()\n"
        )
        (installed / "data.txt").write_text("package data")
        (site / "dynamicmod.py").write_text("value = 23\n")
        metadata = site / "installedpkg-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: installedpkg\nVersion: 1.0\n")
        (metadata / "top_level.txt").write_text("installedpkg\n")
        (metadata / "RECORD").write_text(
            "installedpkg/__init__.py,,\ninstalledpkg/data.txt,,\n"
            "installedpkg-1.0.dist-info/METADATA,,\n"
            "installedpkg-1.0.dist-info/top_level.txt,,\n"
            "installedpkg-1.0.dist-info/RECORD,,\n"
        )
        self.env["PYTHONPATH"] = str(site)
        source = project / "test.py"
        source.write_text(
            "import ctypes, importlib, importlib.metadata, os, sqlite3, ssl\n"
            "from localpkg import answer\n"
            "from namespacepkg.child import value\n"
            "import namespace_data\nfrom pathlib import Path\n"
            "import installedpkg\n"
            "try:\n    import unavailable_optional_vmtest_module\nexcept ImportError:\n    pass\n"
            "assert __name__ == '__main__'\n"
            "assert os.getpid() == 1\n"
            "assert answer == 42 and value == 17\n"
            "assert installedpkg.value == 'package data'\n"
            "assert Path(namespace_data.__path__[0], 'data.txt').read_text() == 'namespace data'\n"
            "assert importlib.metadata.version('installedpkg') == '1.0'\n"
            "assert importlib.import_module('dynamic' + 'mod').value == 23\n"
            "assert sqlite3.connect(':memory:').execute('select 42').fetchone() == (42,)\n"
            "assert ssl.OPENSSL_VERSION\n"
            "print('RESULT PASS', flush=True)\n"
            "os.sync()\n"
            "ctypes.CDLL(None).reboot(0x4321FEDC)\n"
        )
        return source, site

    def test_python_imports_data_metadata_and_native_dependencies(self):
        source, site = self.make_python_fixture()
        result, root = self.run_vmtest(source, "--python-module", "dynamicmod")
        self.assertIn("packed Python", result.stdout)
        self.assertTrue((root / "vmtest-python").is_symlink())
        for path in (source, source.parent / "localpkg/values.py", source.parent / "namespacepkg/child.py",
                     site / "installedpkg/data.txt", site / "installedpkg-1.0.dist-info/METADATA",
                     site / "dynamicmod.py"):
            self.assertTrue((root / str(path).lstrip("/")).is_file(), str(path))
        self.assertFalse((self.directory / "imported-on-host").exists())
        self.assertTrue(list(root.rglob("_ssl*.so")))
        self.assertTrue(list(root.rglob("libssl.so*")))
        self.assertTrue(list(root.rglob("libsqlite3.so*")))

    def test_python_shebang_without_extension(self):
        source = self.directory / "script"
        source.write_text("#!/usr/bin/env -S python3 -B\nprint('RESULT PASS')\n")
        result, root = self.run_vmtest(source)
        self.assertIn("packed Python", result.stdout)
        self.assertTrue((root / "init").read_text().startswith("#!/vmtest-python -u\n"))

    def test_virtualenv_and_command_line_override_of_config(self):
        venv = self.directory / "venv with spaces"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        interpreter = venv / "bin/python"
        site = Path(subprocess.check_output(
            [str(interpreter), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
        ).strip())
        (site / "venvmodule.py").write_text("value = 99\n")
        module = Path(subprocess.check_output(
            [str(interpreter), "-c", "import importlib.util; print(importlib.util.find_spec('venvmodule').origin)"],
            text=True,
        ).strip())
        source = self.directory / "script"
        source.write_text(
            "#!/nonexistent/python3\nimport ctypes, os, venvmodule\n"
            "assert os.getpid() == 1 and venvmodule.value == 99\n"
            "print('RESULT PASS', flush=True)\nctypes.CDLL(None).reboot(0x4321FEDC)\n"
        )
        Path(str(source) + ".vmtest").write_text("--python /nonexistent/config-python\n")
        live = bool(os.environ.get("VMTEST_KERNEL"))
        result, root = self.run_vmtest(source, "--python", interpreter, live=live)
        self.assertTrue((root / str(module).lstrip("/")).is_file())
        if live:
            self.assertIn("QEMU exit status 0", result.stdout)
            self.assertNotIn("Kernel panic", result.stdout)

    @unittest.skipUnless(Path(sysconfig.get_path("include"), "Python.h").is_file(), "requires Python headers")
    def test_native_extension_dependency_in_path_with_spaces(self):
        libraries = self.directory / "native libraries"
        libraries.mkdir()
        library_source = libraries / "value.c"
        library_source.write_text("int vmtest_value(void) { return 42; }\n")
        subprocess.run(["gcc", "-shared", "-fPIC", "-Wl,-soname,libvmtestnative.so",
                        "-o", str(libraries / "libvmtestnative.so"), str(library_source)], check=True)
        extension_source = self.directory / "nativeplug.c"
        extension_source.write_text(
            '#include <Python.h>\nextern int vmtest_value(void);\n'
            'static PyObject *value(PyObject *s, PyObject *a) { return PyLong_FromLong(vmtest_value()); }\n'
            'static PyMethodDef methods[] = {{"value", value, METH_NOARGS, NULL}, {NULL}};\n'
            'static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "nativeplug", NULL, -1, methods};\n'
            'PyMODINIT_FUNC PyInit_nativeplug(void) { return PyModule_Create(&module); }\n'
        )
        subprocess.run(["gcc", "-shared", "-fPIC", "-I" + sysconfig.get_path("include"),
                        "-o", str(self.directory / "nativeplug.so"), str(extension_source),
                        "-L" + str(libraries), "-lvmtestnative", "-Wl,-rpath," + str(libraries)], check=True)
        source = self.directory / "native.py"
        source.write_text(
            "import ctypes, nativeplug\nassert nativeplug.value() == 42\n"
            "print('RESULT PASS', flush=True)\nctypes.CDLL(None).reboot(0x4321FEDC)\n"
        )
        live = bool(os.environ.get("VMTEST_KERNEL"))
        result, root = self.run_vmtest(source, "--python", sys.executable, live=live)
        self.assertTrue(list(root.rglob("libvmtestnative.so")))
        if live:
            self.assertIn("QEMU exit status 0", result.stdout)
            self.assertNotIn("Kernel panic", result.stdout)

    def test_missing_explicit_module_fails_before_boot(self):
        source = self.directory / "test.py"
        source.write_text("print('RESULT PASS')\n")
        result, _ = self.run_vmtest(source, "--python-module", "nonexistent_vmtest_module", success=False)
        self.assertIn("Python module not found: nonexistent_vmtest_module", result.stdout)
        self.assertNotIn("booting", result.stdout)

    def test_c_and_dynamic_binary_still_work(self):
        source = self.directory / "test.c"
        source.write_text('#include <stdio.h>\nint main(void) { puts("RESULT PASS"); return 0; }\n')
        _, root = self.run_vmtest(source)
        self.assertEqual((root / "init").read_bytes()[:4], b"\x7fELF")
        binary = self.directory / "test"
        subprocess.run(["gcc", "-o", str(binary), str(source)], check=True)
        _, root = self.run_vmtest(binary)
        self.assertEqual((root / "init").read_bytes(), binary.read_bytes())
        self.assertTrue(list(root.rglob("libc.so*")))

    @unittest.skipUnless(os.environ.get("VMTEST_KERNEL") and shutil.which("qemu-system-x86_64"),
                         "set VMTEST_KERNEL to boot a real VM")
    def test_python_in_real_vm(self):
        source, _ = self.make_python_fixture()
        result, _ = self.run_vmtest(source, "--python-module", "dynamicmod", live=True)
        self.assertIn("QEMU exit status 0", result.stdout)
        self.assertNotIn("Kernel panic", result.stdout)


if __name__ == "__main__":
    unittest.main()
