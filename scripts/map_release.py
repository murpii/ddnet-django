#!/home/django/ddnet-django/.venv/bin/python

import os
import sys
import json
from shutil import copyfile
from tempfile import TemporaryDirectory
import subprocess

from PIL import Image


def crop_preview(image):
    width, height = image.size
    if width < 8 or height < 5:
        return image

    target_ratio = 8 / 5
    current_ratio = width / height

    if current_ratio > target_ratio:
        cropped_width = int(height * target_ratio)
        left = (width - cropped_width) // 2
        return image.crop((left, 0, left + cropped_width, height))
    if current_ratio < target_ratio:
        cropped_height = int(width / target_ratio)
        top = (height - cropped_height) // 2
        return image.crop((0, top, width, top + cropped_height))
    return image


def release():
    with TemporaryDirectory() as tempdir:
        maps = json.load(sys.stdin)

        for m, d in maps.items():
            mappath = os.path.join(tempdir, os.path.basename(d['map']))
            copyfile(d['map'], mappath)

            # generate msgpack
            p = subprocess.Popen(
                ['map_properties', mappath, os.path.join(tempdir, m + '.msgpack')],
                stdout=sys.stdout,
                stderr=sys.stderr
            )
            if p.wait() != 0:
                print('map_properties terminated with error.')
                return 1

            # generate image
            impath = os.path.join(tempdir, os.path.basename(d['image']))
            im = Image.open(d['image'])
            im = crop_preview(im)

            im.thumbnail((360, 225))
            im.save(impath)

            p = subprocess.Popen(
                ['zopflipng', '-m', '-y', impath, impath],
                stdout=sys.stdout,
                stderr=sys.stderr
            )
            if p.wait() != 0:
                print('zopflipng terminated with error.')
                return 2

        # generate map type listings
        types_dir = os.path.join(tempdir, 'types')
        os.mkdir(types_dir)
        server_types = {d['server_type'] for d in maps.values()}

        for st in server_types:
            outpath = os.path.join(types_dir, st.lower())
            with open(outpath, 'wb') as out:
                p = subprocess.Popen(
                    [sys.executable, os.path.join(os.path.dirname(__file__), 'print_mapfile.py'), st],
                    stdout=out,
                    stderr=sys.stderr
                )
                if p.wait() != 0:
                    print('print_mapfile terminated with error.')
                    return 3

        # ensure tempdir is world-readable
        subprocess.call(['chmod', 'a+x', tempdir])
        subprocess.call(['chmod', '-R', 'a+r', tempdir])

        # finalize release
        p = subprocess.Popen(
            ['map_release_final', tempdir],
            stdout=sys.stdout,
            stderr=sys.stderr
        )
        if p.wait() != 0:
            print('map_release_final terminated with error.')
            return 4

    return 0


if __name__ == "__main__":
    sys.exit(release())
