"""Views for the EmbedAPI v3 app."""

import logging
import re
from urllib.parse import urlparse
import requests

from selectolax.parser import HTMLParser
from pyquery import PyQuery as PQ  # noqa

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils.functional import cached_property
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from readthedocs.api.v2.mixins import CachedResponseMixin
from readthedocs.core.unresolver import unresolve
from readthedocs.core.utils.extend import SettingsOverrideObject
from readthedocs.embed.utils import clean_links
from readthedocs.projects.constants import PUBLIC
from readthedocs.storage import build_media_storage

log = logging.getLogger(__name__)



class EmbedAPIBase(CachedResponseMixin, APIView):

    # pylint: disable=line-too-long

    """
    Embed a section of content from any Read the Docs page.

    ### Arguments

    * url (with fragment) (required)
    * doctool
    * doctoolversion

    ### Example

    GET https://readthedocs.org/api/v3/embed/?url=https://docs.readthedocs.io/en/latest/features.html%23#full-text-search

    """  # noqa

    permission_classes = [AllowAny]
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer]

    @cached_property
    def unresolved_url(self):
        url = self.request.GET.get('url')
        if not url:
            return None
        return unresolve(url)

    def _download_page_content(self, url):
        cached_response = cache.get(url)
        if cached_response:
            log.debug('Cached response. url=%s', url)
            return cached_response

        response = requests.get(url)
        if response.ok:
            cache.set(url, response.text, timeout=60 * 5)
            return response.text

    def _get_page_content_from_storage(self):
        project = self.unresolved_url.project
        version = get_object_or_404(
            project.versions,
            slug=self.unresolved_url.version_slug,
            # Only allow PUBLIC versions when getting the content from our storage
            privacy_level=PUBLIC,
        )
        storage_path = project.get_storage_path(
            'html',
            version_slug=version.slug,
            include_file=False,
            version_type=version.type,
        )
        file_path = build_media_storage.join(
            storage_path,
            self.unresolved_url.filename,
        )
        try:
            with build_media_storage.open(file_path) as fd:
                return fd.read()
        except Exception:  # noqa
            log.warning('Unable to read file. file_path=%s', file_path)

        return None

    def _get_content_by_fragment(self, url, fragment, external):
        if external:
            url_without_fragment = urlparse(url)._replace(fragment='').geturl()
            page_content = self._download_page_content(url_without_fragment)
        else:
            page_content = self._get_page_content_from_storage()
        node = HTMLParser(page_content).css_first(f'#{fragment}')
        if node:
            return node.html

    def get(self, request):
        url = request.GET.get('url')
        doctool = request.GET.get('doctool')
        doctoolversion = request.GET.get('doctoolversion')
        if not url:
            return Response(
                {
                    'error': (
                        'Invalid arguments. '
                        'Please provide "url".'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        if not all([doctool, doctoolversion]) and any([doctool, doctoolversion]):
            return Response(
                {
                    'error': (
                        'Invalid arguments. '
                        'Please provide "doctool" and "doctoolversion" or none of them.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # Note that ``readthedocs.core.unresolver.unresolve`` returns ``None``
        # when it can find the project in our database
        external = self.unresolved_url is None

        parsed_url = urlparse(url)
        external_domain = parsed_url.netloc
        if external and external_domain:
            allowed_domain = False
            for domain in settings.RTD_EMBED_API_EXTERNAL_DOMAINS:
                if re.match(domain, external_domain):
                    allowed_domain = True
                    break

            if not allowed_domain:
                return Response(
                    {
                        'error': (
                            'External domain not allowed. '
                            f'domain={external_domain}'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Check rate-limit for this particular domain
            cache_key = f'embed-api-{external_domain}'
            cache.get_or_set(cache_key, 0, timeout=60)
            cache.incr(cache_key)
            if cache.get(cache_key) > settings.RTD_EMBED_API_DOMAIN_RATE_LIMIT:
                log.info('Too many requests for this domain. domain=%s', external_domain)
                return Response(
                    {
                        'error': (
                            'Too many requests for this domain. '
                            f'domain={external_domain}'
                        )
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        fragment = parsed_url.fragment
        content_requested = self._get_content_by_fragment(url, fragment, external)
        if not content_requested:
            return Response(
                {
                    'error': (
                        "Can't find content for section: "
                        f"url={url} fragment={fragment}"
                    )
                },
                status=status.HTTP_404_NOT_FOUND
            )

        response = {
            'url': url,
            'fragment': fragment,
            'content': clean_links(
                content_requested,
                url,
                html_raw_response=True,
            ),
            'external': external,
        }

        if not external:
            response.update({
                'project': self.unresolved_url.project.slug,
                'version': self.unresolved_url.version_slug,
                'language': self.unresolved_url.language_slug,
                'path': self.unresolved_url.filename,
            })
        return Response(response)


class EmbedAPI(SettingsOverrideObject):
    _default_class = EmbedAPIBase
