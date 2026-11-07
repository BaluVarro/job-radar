"""HR-XML 3 job feeds (used by e.g. karrier.erstebank.hu/job/getListXml/...)."""

from __future__ import annotations

from lxml import etree

from .. import http
from ..models import Job
from .base import Adapter


def _first(node, local_name: str) -> str:
    found = node.xpath(f'.//*[local-name()="{local_name}"]/text()')
    return found[0].strip() if found else ""


class HrXmlAdapter(Adapter):
    name = "hrxml"

    def fetch(self) -> list[Job]:
        resp = http.get(self.session, self.company["url"])
        return self.parse(resp.content)

    def parse(self, content: bytes) -> list[Job]:
        root = etree.fromstring(content, parser=etree.XMLParser(recover=True))
        jobs = []
        for opening in root.xpath('//*[local-name()="PositionOpening"]'):
            title = _first(opening, "PositionTitle")
            link = _first(opening, "Link")
            start = opening.xpath('.//*[local-name()="StartDate"]'
                                  '/*[local-name()="FormattedDateTime"]/text()')
            city = _first(opening, "CityName")
            jobs.append(self.make_job(
                title=title,
                url=link,
                location=city,
                posted=start[0].strip()[:10] if start else "",
            ))
        return jobs
