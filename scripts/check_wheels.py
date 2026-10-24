import email
import re
import sys
from pathlib import Path
from zipfile import ZipFile

import tomllib

PACKAGE_FILES = {
    'urio/__init__.py',
    'urio/_version.py',
    'urio/backend.py',
    'urio/driver.py',
    'urio/driver_iocp.py',
    'urio/driver_iocp_proactor.py',
    'urio/file.py',
    'urio/path.py',
    'urio/py.typed',
}
VENDORED_LIBRARY = re.compile(
    r'urio\.libs/lib[A-Za-z0-9_+.-]+-[0-9a-f]{8}\.so(?:\.\d+)*'
)


def check_wheel(path: Path, version: str) -> None:
    metadata_dir = f'urio-{version}.dist-info/'
    with ZipFile(path) as wheel:
        names = {item.filename for item in wheel.infolist() if not item.is_dir()}
        native_modules = {
            name
            for name in names
            if name.startswith('urio/_uring.')
            and name.endswith(('.so', '.pyd'))
            and '/' not in name[len('urio/') :]
        }
        vendored_libraries = {
            name
            for name in names
            if 'linux' in path.name and VENDORED_LIBRARY.fullmatch(name)
        }
        metadata_files = {
            metadata_dir + name for name in ('METADATA', 'WHEEL', 'RECORD')
        }
        metadata_files |= {
            name
            for name in names
            if name.startswith(metadata_dir + 'licenses/')
            or name.startswith(metadata_dir + 'sboms/')
        }
        expected = PACKAGE_FILES | native_modules | vendored_libraries | metadata_files
        unexpected = names - expected
        missing = PACKAGE_FILES - names
        missing |= {
            metadata_dir + name for name in ('METADATA', 'WHEEL', 'RECORD')
        } - names
        if len(native_modules) != 1:
            raise ValueError(
                f'{path}: expected one native extension, got {native_modules}'
            )
        if unexpected or missing:
            raise ValueError(
                f'{path}: unexpected={sorted(unexpected)}, missing={sorted(missing)}'
            )
        if any(
            not wheel.read(name).startswith(b'\x7fELF') for name in vendored_libraries
        ):
            raise ValueError(f'{path}: invalid vendored shared library')
        metadata = email.message_from_bytes(wheel.read(metadata_dir + 'METADATA'))
        if metadata['Name'] != 'urio' or metadata['Version'] != version:
            raise ValueError(f'{path}: incorrect package metadata')
        classifiers = metadata.get_all('Classifier', [])
        for classifier in (
            'Development Status :: 5 - Production/Stable',
            'Programming Language :: Python :: 3.15',
        ):
            if classifier not in classifiers:
                raise ValueError(f'{path}: missing classifier {classifier}')
        print(f'{path}: {len(names)} files, {path.stat().st_size:,} bytes')


def main() -> None:
    version = tomllib.loads(Path('Cargo.toml').read_text())['package']['version']
    wheels = sorted(Path(sys.argv[1]).rglob('*.whl'))
    if not wheels:
        raise ValueError(f'no wheels found in {sys.argv[1]}')
    for wheel in wheels:
        check_wheel(wheel, version)


if __name__ == '__main__':
    main()
