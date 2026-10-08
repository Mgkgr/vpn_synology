"""Export only reviewed Python sources and pinned image metadata, never runtime data."""
import argparse
import hashlib
from pathlib import Path
import tarfile


def package(destination):
    root=Path(__file__).resolve().parent
    files=sorted(list((root/'maintenance').glob('*.py'))+list((root/'antidpi').glob('*.py')))
    files.append(root/'antidpi/versions.json')
    with tarfile.open(destination,'w',format=tarfile.USTAR_FORMAT) as archive:
        for path in files:
            if path.is_symlink() or path.stat().st_size>1048576: raise ValueError('invalid_source')
            path.read_text(encoding='utf-8')
            archive.add(path,arcname='deploy/'+path.relative_to(root).as_posix(),recursive=False)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('destination',type=Path)
    print(package(parser.parse_args().destination))
