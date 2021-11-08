# coding=utf-8
import asyncio
import os
import random
import json
from collections import defaultdict
from typing import (
    AnyStr,
    List,
    Dict,
    Tuple,
)

from aiohttp import ClientSession, TCPConnector
from sqlalchemy.orm import Session, joinedload, noload

from api.exceptions import UnknownRepositoryOrVersion
from api.mirrors_update import (
    get_config,
    get_mirrors_info,
    update_mirror_in_db,
)
from api.redis import (
    get_mirrors_from_cache,
    set_mirrors_to_cache,
    get_url_types_from_cache,
    set_url_types_to_cache,
    get_mirror_flapped,
    set_mirror_list,
    get_mirror_list
)
from api.utils import (
    get_geo_data_by_ip,
    get_aws_subnets,
    get_azure_subnets,
    set_subnets_for_hyper_cloud_mirror,
    sort_mirrors_by_distance,
    randomize_mirrors_within_distance
)
from db.data_models import (
    RepoData,
    MainConfig,
)
from db.models import (
    Url,
    Mirror,
    MirrorData,
    get_asn_by_ip,
    is_ip_in_any_subnet,
    Subnet,
)
from db.data_models import MirrorYamlData
from db.utils import session_scope
from sqlalchemy.sql.expression import (
    null,
    false,
    or_,
)
from common.sentry import (
    get_logger,
)

logger = get_logger(__name__)


LENGTH_GEO_MIRRORS_LIST = 10
LENGTH_CLOUD_MIRRORS_LIST = 5


async def _get_nearest_mirrors_by_network_data(
        ip_address: AnyStr,
) -> List[MirrorData]:
    """
    The function returns mirrors which are in the same subnet or have the same
    ASN as a request's IP
    """
    match = get_geo_data_by_ip(ip_address)
    asn = get_asn_by_ip(ip_address)
    suitable_mirrors = []
    mirrors = await get_mirror_list()
    if not mirrors:
        await refresh_mirrors_cache()
        mirrors = await get_mirror_list()
    for mirror in mirrors:
        if mirror.status != "ok":
            continue
        if (asn and asn == mirror.asn) or is_ip_in_any_subnet(
            ip_address=ip_address,
            subnets=mirror.subnets,
        ):
            suitable_mirrors.append(mirror.to_dataclass())
    if 1 <= len(suitable_mirrors) < LENGTH_CLOUD_MIRRORS_LIST\
            and match is not None:
        continent, country, _, _, latitude, longitude = match

        for mirror in mirrors:
            if mirror.name in [mirror.name for mirror in suitable_mirrors]:
                continue
            suitable_mirrors.extend(mirror)

        suitable_mirrors = randomize_mirrors_within_distance(
            sort_mirrors_by_distance(
                (latitude, longitude),
                suitable_mirrors
            )
        )
    return suitable_mirrors


async def _get_nearest_mirrors_by_geo_data(
        ip_address: AnyStr,
        empty_for_unknown_ip: bool = False,
) -> List[MirrorData]:
    """
    The function returns N nearest mirrors towards a request's IP
    Firstly, it searches first N mirrors inside a request's country
    Secondly, it searches first N nearest mirrors by distance
        inside a request's continent
    Thirdly, it searches first N nearest mirrors by distance in the world
    Further the functions concatenate lists and return first
        N elements of a summary list
    :param empty_for_unknown_ip: if True and we can't get geo data of an IP
        the function returns empty list
    """
    match = get_geo_data_by_ip(ip_address)
    mirrors = [mirror for mirror in await get_mirror_list() if mirror.cloud_type == '' and mirror.status == "ok"]
    if not mirrors:
        await refresh_mirrors_cache()
        mirrors = [mirror for mirror in await get_mirror_list() if mirror.cloud_type == '' and mirror.status == "ok"]
    # We return all of mirrors if we can't
    # determine geo data of a request's IP
    if match is None:
        return mirrors
    continent, country, state, city, latitude, longitude = match


    # TODO: SQLAlchemy adds brackets around queries. And it looks like
    # TODO: incorrect query for SQLite
    # suitable_mirrors_query = mirrors_by_country_query.union_all(
    #     mirrors_by_continent_query,
    # ).union_all(
    #     all_rest_mirrors_query,
    # ).limit(MAX_LENGTH_OF_MIRRORS_LIST)
    # suitable_mirrors = suitable_mirrors_query.all()

    # sort mirrors by distance and randomize those within specified distance
    # to avoid the same mirrors handling the majority of traffic especially
    # within larger cities
    if city or state:
        mirrors = randomize_mirrors_within_distance(
            sort_mirrors_by_distance(
                (latitude, longitude),
                mirrors
            )
        )
    # if we don't have city or state data for a requesting IP then geoip isn't
    # very accurate anyway so let's give it a random mirror to spread the load.
    # many IPs are missing this data and this prevents all of those requests from
    # disproportionately hitting mirrors near the geographical center of the US
    else:
        mirrors = [mirror for mirror in mirrors if mirror.cloud_type == '' and mirror.status == "ok"]
        # TODO prefer mirrors from the same country, then continent first if possible.
        random.shuffle(mirrors)

    suitable_mirrors = mirrors

    return suitable_mirrors[:LENGTH_GEO_MIRRORS_LIST]


async def _get_nearest_mirrors(
        ip_address: AnyStr,
        empty_for_unknown_ip: bool = False,
) -> List[MirrorData]:
    """
    Get nearest mirrors by geo-data or by subnet/ASN
    """
    suitable_mirrors = await get_mirrors_from_cache(ip_address)
    if suitable_mirrors is not None:
        return suitable_mirrors
    suitable_mirrors = await _get_nearest_mirrors_by_network_data(
        ip_address=ip_address,
    )
    if not suitable_mirrors:
        suitable_mirrors = await _get_nearest_mirrors_by_geo_data(
            ip_address=ip_address,
            empty_for_unknown_ip=empty_for_unknown_ip,
        )
    await set_mirrors_to_cache(
        ip_address,
        suitable_mirrors,
    )
    return suitable_mirrors


async def _process_mirror(
        subnets: Dict[AnyStr, List[AnyStr]],
        mirror_info: MirrorYamlData,
        versions: List[AnyStr],
        repos: List[RepoData],
        allowed_outdate: AnyStr,
        db_session: Session,
        http_session: ClientSession,
        arches: List[AnyStr],
        required_protocols: List[AnyStr],
        nominatim_sem: asyncio.Semaphore,
        mirror_check_sem: asyncio.Semaphore
):
    set_subnets_for_hyper_cloud_mirror(
        subnets=subnets,
        mirror_info=mirror_info,

    )
    async with mirror_check_sem:
        await update_mirror_in_db(
            mirror_info=mirror_info,
            versions=versions,
            repos=repos,
            allowed_outdate=allowed_outdate,
            db_session=db_session,
            http_session=http_session,
            arches=arches,
            required_protocols=required_protocols,
            sem=nominatim_sem
        )


async def update_mirrors_handler() -> AnyStr:
    config = get_config()
    mirrors_dir = os.path.join(
        os.getenv('CONFIG_ROOT'),
        'mirrors/updates',
        config.mirrors_dir,
    )
    all_mirrors = get_mirrors_info(
        mirrors_dir=mirrors_dir,
    )

    # semaphore for nominatim
    nominatim_sem = asyncio.Semaphore(1)

    with session_scope() as db_session:
        db_session.query(Mirror).delete()
        db_session.query(Url).delete()
        db_session.query(Subnet).delete()
        len_list = len(all_mirrors)
        mirror_check_sem = asyncio.Semaphore(100)
        conn = TCPConnector(limit=10000, force_close=True)
        async with ClientSession(connector=conn, headers={"Connection": "close"}) as http_session:
            subnets = await get_aws_subnets(http_session=http_session)
            subnets.update(await get_azure_subnets(http_session=http_session))
            await asyncio.gather(*(
                asyncio.ensure_future(
                    _process_mirror(
                        subnets=subnets,
                        mirror_info=mirror_info,
                        versions=config.versions,
                        repos=config.repos,
                        allowed_outdate=config.allowed_outdate,
                        db_session=db_session,
                        http_session=http_session,
                        arches=config.arches,
                        required_protocols=config.required_protocols,
                        nominatim_sem=nominatim_sem,
                        mirror_check_sem=mirror_check_sem,
                    )
                ) for mirror_info in all_mirrors
            ))
        db_session.flush()
    await refresh_mirrors_cache()
    return 'Done'


async def refresh_mirrors_cache() -> AnyStr:
    mirrors = await get_all_mirrors_db()
    mirror_list = []
    for mirror in mirrors:
        mirror_list.append(mirror.to_json())
    await set_mirror_list(mirrors=mirror_list)
    return 'Done'


async def get_all_mirrors() -> List[MirrorData]:
    mirrors = await get_mirror_list()
    if not mirrors:
        await refresh_mirrors_cache()
        mirrors = await get_mirror_list()

    return [mirror for mirror in mirrors]


async def get_all_mirrors_db() -> List[MirrorData]:
    mirrors_list = []
    with session_scope() as session:
        mirrors_query = session.query(
            Mirror
        ).options(
            joinedload(Mirror.urls),
            joinedload(Mirror.subnets)
        ).order_by(
            Mirror.continent,
            Mirror.country,
        )
        mirrors_query = mirrors_query.filter(
            or_(
                Mirror.private == false(),
                Mirror.private == null()
            ),
        )
        mirrors = mirrors_query.all()
        for mirror in mirrors:
            mirror_data = mirror.to_dataclass()
            mirrors_list.append(mirror_data)
    return mirrors_list


async def get_mirrors_list(
        ip_address: AnyStr,
        version: AnyStr,
        repository: AnyStr,
) -> AnyStr:
    mirrors_list = []
    config = get_config()
    versions = [str(version) for version in config.versions]
    if version not in versions:
        try:
            version = next(ver for ver in versions if version.startswith(ver))
        except StopIteration:
            raise UnknownRepositoryOrVersion(
                'Unknown version "%s". Allowed list of versions "%s"',
                version,
                ', '.join(versions),
            )
    repos = {
        repo.name: repo for repo in config.repos
    }  # type: Dict[AnyStr, RepoData]
    if repository not in repos:
        raise UnknownRepositoryOrVersion(
            'Unknown repository "%s". Allowed list of repositories "%s"',
            repository,
            ', '.join(repos.keys()),
        )
    repo_path = repos[repository].path
    nearest_mirrors = await _get_nearest_mirrors(ip_address=ip_address)
    for mirror in nearest_mirrors:
        mirror_url = mirror.urls.get(config.required_protocols[0]) or \
                     mirror.urls.get(config.required_protocols[1])
        full_mirror_path = os.path.join(
            mirror_url,
            version,
            repo_path
        )
        mirrors_list.append(full_mirror_path)

    return '\n'.join(mirrors_list)


def _set_isos_link_for_mirror(
        mirror_info: MirrorData,
        version: AnyStr,
        arch: AnyStr,
        config: MainConfig,
):
    urls = mirror_info.urls
    mirror_url = next(
        address for protocol_type, address in
        urls.items()
        if protocol_type in config.required_protocols
    )
    mirror_info.isos_link = os.path.join(
        mirror_url,
        str(version),
        'isos',
        arch,
    )


async def get_isos_list_by_countries(
        arch: AnyStr,
        version: AnyStr,
        ip_address: AnyStr,
        config: MainConfig,
) -> Tuple[Dict[AnyStr, List[MirrorData]], List[MirrorData]]:
    mirrors_by_countries = defaultdict(list)
    for mirror_info in await get_all_mirrors():
        # Hyper clouds (like AWS/Azure) don't have isos, because they traffic
        # is too expensive
        if mirror_info.cloud_type in ('aws', 'azure'):
            continue

        _set_isos_link_for_mirror(
            mirror_info=mirror_info,
            version=version,
            arch=arch,
            config=config,
        )
        mirrors_by_countries[mirror_info.country].append(mirror_info)
    nearest_mirrors = await _get_nearest_mirrors(
        ip_address=ip_address,
        empty_for_unknown_ip=True,
    )
    for nearest_mirror in nearest_mirrors:
        # Hyper clouds (like AWS/Azure) don't have isos, because they traffic
        # is too expensive
        if nearest_mirror.cloud_type in ('aws', 'azure'):
            continue
        _set_isos_link_for_mirror(
            mirror_info=nearest_mirror,
            version=version,
            arch=arch,
            config=config,
        )
    return mirrors_by_countries, nearest_mirrors


def get_main_isos_table(config) -> Dict[AnyStr, List[AnyStr]]:
    result = defaultdict(list)
    for arch in config.arches:
        result[arch] = [version for version in config.versions
                        if version not in config.duplicated_versions]

    return result


async def get_url_types() -> List[AnyStr]:
    url_types = await get_url_types_from_cache()
    if url_types is not None:
        return url_types
    with session_scope() as session:
        url_types = sorted(value[0] for value in session.query(
            Url.type
        ).distinct())
        await set_url_types_to_cache(url_types)
        return url_types
