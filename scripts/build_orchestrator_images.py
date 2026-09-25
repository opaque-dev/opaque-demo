#!/usr/bin/env python3
"""Build source-pinned Opaque, Airflow and Temporal demo images locally."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from ax_minikube_demo import CORE_REVISION, ROOT


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-repo', required=True, type=Path)
    parser.add_argument('--tag', default='local')
    args = parser.parse_args()
    if not re.fullmatch('[a-zA-Z0-9_.-]+', args.tag):
        raise ValueError('invalid image tag')
    with tempfile.TemporaryDirectory(prefix='opaque-orchestrators-build-') as directory:
        context = Path(directory)
        archive = context / 'core.tar'
        run('git', '-C', args.core_repo.resolve(), 'archive', '--format=tar', '-o', archive, CORE_REVISION)
        source = context / 'source'
        source.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter='data')
        original = ROOT / 'examples/ax-minikube'
        for relative in ('broker/install.sh', 'fixtures/client.py', 'fixtures/services.py'):
            dest = context / 'bootstrap' / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original / relative, dest)
        broker = 'opaque-orchestrators-broker:' + args.tag
        example = ROOT / 'examples/orchestrators'
        run('docker', 'build', '-f', example / 'broker/Dockerfile', '--build-arg', 'OPAQUE_BUILD_REVISION=' + CORE_REVISION,
            '-t', broker, context)
        for platform in ('airflow', 'temporal'):
            run('docker', 'build', '-f', example / platform / 'Dockerfile', '--build-arg', 'OPAQUE_IMAGE=' + broker,
                '-t', 'opaque-' + platform + ':' + args.tag, example)
        print('Built:', broker, 'opaque-airflow:' + args.tag, 'opaque-temporal:' + args.tag)


if __name__ == '__main__':
    main()
