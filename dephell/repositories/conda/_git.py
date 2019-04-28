import datetime
import os
import re
import sys
import time
from logging import getLogger
from platform import uname, python_version
from types import MappingProxyType, SimpleNamespace
from typing import Dict, List, Any, Optional

import asyncio
import attr
import requests
from aiohttp import ClientSession
from dephell_specifier import RangeSpecifier
from jinja2 import Environment
from ruamel.yaml import YAML

from ...cache import JSONCache
from ...config import config
from ...models.release import Release
from ...models.simple_dependency import SimpleDependency
from ...utils import cached_property
from ._base import CondaBaseRepo


try:
    import yaml as pyyaml
except ImportError:
    pyyaml = None


# source: conda-build/metadata.py
# Selectors must be either:
# - at end of the line
# - embedded (anywhere) within a comment
#
# Notes:
# - [([^\[\]]+)\] means "find a pair of brackets containing any
#                 NON-bracket chars, and capture the contents"
# - (?(2)[^\(\)]*)$ means "allow trailing characters iff group 2 (#.*) was found."
#                 Skip markdown link syntax.
REX_SELECTOR = re.compile(r'(.+?)\s*(#.*)?\[([^\[\]]+)\](?(2)[^\(\)]*)$')


HISTORY_URL = 'https://api.github.com/repos/{repo}/commits?path={path}&per_page=100'
CONTENT_URL = 'https://raw.githubusercontent.com/{repo}/{rev}/{path}'


logger = getLogger('dephell.repositories.conda')
loop = asyncio.get_event_loop()


@attr.s()
class CondaGitRepo(CondaBaseRepo):
    channels = attr.ib(type=List[str], factory=list)

    cookbooks = MappingProxyType({
        # https://github.com/conda-forge/textdistance-feedstock/blob/master/recipe/meta.yaml
        # https://github.com/conda-forge/ukbparse-feedstock/blob/master/recipe/meta.yaml
        'conda-forge': dict(repo='conda-forge/{name}-feedstock', path='recipe/meta.yaml'),
        # https://github.com/bioconda/bioconda-recipes/blob/master/recipes/anvio/meta.yaml
        'bioconda': dict(repo='bioconda/bioconda-recipes', path='recipes/{name}/meta.yaml'),
    })

    def get_releases(self, dep) -> tuple:
        revs = self._get_revs(name=dep.name)
        releases = dict()

        # get metainfo
        cache = JSONCache('conda', 'git', dep.name, ttl=config['cache']['ttl'])
        raw_releases = cache.load()
        if raw_releases is None:
            coroutines = []
            for rev in revs:
                coroutines.append(self._get_meta(
                    rev=rev['rev'],
                    repo=rev['repo'],
                    path=rev['path'],
                ))
            gathered = asyncio.gather(*coroutines)
            raw_releases = loop.run_until_complete(gathered)
            cache.dump(raw_releases)

        for meta in raw_releases:
            if meta is None:
                continue
            version = str(meta['package']['version'])
            if version in releases:
                continue

            # make release
            release = Release(
                raw_name=dep.raw_name,
                version=version,
                time=rev['time'],
            )
            digest = meta.get('source', {}).get('sha256')
            if digest:
                release.hashes = (digest, )

            # get deps
            deps = []
            for req in meta.get('requirements', {}).get('run', []):
                parsed = self.parse_req(req)
                if parsed['name'] == 'python':
                    release.python = RangeSpecifier(parsed.get('version', '*'))
                    continue
                req = parsed['name'] + parsed.get('version', '')
                deps.append(SimpleDependency(
                    name=parsed['name'],
                    specifier=parsed.get('version', '*'),
                ))
            release.dependencies = tuple(deps)

            releases[version] = release

        return tuple(sorted(releases.values(), reverse=True))

    async def get_dependencies(self, *args, **kwargs):
        raise NotImplementedError('use get_releases to get deps')

    # hidden methods

    def _get_revs(self, name: str) -> List[Dict[str, str]]:
        cookbooks = []
        for channel in self.channels:
            cookbook = self.cookbooks.get(channel)
            if cookbook:
                cookbooks.append(dict(
                    repo=cookbook['repo'].format(name=name),
                    path=cookbook['path'].format(name=name),
                ))
        if not cookbooks:
            cookbook = self.cookbooks['conda-forge']
            cookbooks.append(dict(
                repo=cookbook['repo'].format(name=name),
                path=cookbook['path'].format(name=name),
            ))

        revs = []
        for cookbook in cookbooks:
            url = HISTORY_URL.format(**cookbook)
            response = requests.get(url)
            if response.status_code != 200:
                continue
            for commit in response.json():
                revs.append(dict(
                    rev=commit['sha'],
                    time=datetime.datetime.strptime(
                        commit['commit']['author']['date'],
                        '%Y-%m-%dT%H:%M:%SZ',
                    ),
                    repo=cookbook['repo'],
                    path=cookbook['path'],
                ))
        return revs

    async def _get_meta(self, rev: str, repo: str, path: str) -> Optional[Dict[str, Any]]:
        # download
        url = CONTENT_URL.format(repo=repo, path=path, rev=rev)
        async with ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise ValueError('invalid response: {} {} ({})'.format(
                        response.status, response.reason, url,
                    ))
                content = await response.text()

        # render
        env = Environment()
        env.globals.update(dict(
            compiler=lambda name: name,
            pin_subpackage=lambda subpackage_name, **kwargs: subpackage_name,
            pin_compatible=lambda subpackage_name, **kwargs: subpackage_name,
            cdt=lambda package_name, **kwargs: package_name + '-cos6-aarch64',
            load_file_regex=lambda *args, **kwargs: None,
            datetime=datetime,
            time=time,
            target_platform='linux-64',
        ))
        template = env.from_string(content)
        content = template.render(
            os=SimpleNamespace(environ=os.environ, sep=os.path.sep),
            environ=os.environ,
        )

        # clean
        lines = []
        for line in content.split('\n'):
            match = REX_SELECTOR.match(line)
            if not match:
                lines.append(line)
                continue
            selector = match.group(3)
            if eval(selector, self._config):
                lines.append(line)
        content = '\n'.join(lines)

        # parse
        yaml = YAML(typ='safe')
        try:
            return yaml.load(content)
        except Exception as e:
            if pyyaml is not None:
                try:
                    return pyyaml.load(content)
                except Exception:
                    pass
            logger.warning('cannot parse recipe', extra=dict(
                url=url,
                error=str(e),
            ))
        return None

    @cached_property
    def _config(self):
        is_64 = sys.maxsize > 2**32
        translation = {
            'Linux': 'linux',
            'Windows': 'win',
            'darwin': 'osx',
        }
        system = translation.get(uname().system, 'linux')
        py = int(''.join(python_version().split('.')[:2]))

        return dict(
            linux=system == 'linux',
            linux32=system == 'linux' and not is_64,
            linux64=system == 'linux' and is_64,
            arm=False,
            osx=system == 'osx',
            unix=system in ('linux', 'osx'),
            win=system == 'win',
            win32=system == 'win' and not is_64,
            win64=system == 'win' and is_64,
            x86=True,
            x86_64=is_64,
            # os=os,
            environ=os.environ,
            nomkl=False,

            py=py,
            py3k=bool(30 <= py < 40),
            py2k=bool(20 <= py < 30),
            py26=bool(py == 26),
            py27=bool(py == 27),
            py33=bool(py == 33),
            py34=bool(py == 34),
            py35=bool(py == 35),
            py36=bool(py == 36),
            py37=bool(py == 37),
            py38=bool(py == 38),
        )