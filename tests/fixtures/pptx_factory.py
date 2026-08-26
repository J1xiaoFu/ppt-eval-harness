"""Create tiny OOXML presentations without depending on PowerPoint."""

from __future__ import annotations

import base64
import html
import zipfile
from pathlib import Path
from typing import Mapping, Sequence

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def build_pptx(
    path: Path,
    slides: Sequence[Sequence[Mapping[str, object]]] | None = None,
    *,
    external_relationship: bool = False,
    active_content: bool = False,
    image_bytes: bytes = PNG_1X1,
) -> Path:
    slides = slides or (
        (
            {"kind": "text", "text": "项目汇报", "x": 600_000, "y": 300_000, "w": 8_000_000, "h": 800_000, "font_pt": 28},
            {"kind": "text", "text": "核心结论：销售额 100 万元", "x": 900_000, "y": 1_600_000, "w": 8_500_000, "h": 1_000_000, "font_pt": 20},
        ),
    )
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Default Extension="png" ContentType="image/png"/>',
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
    ]
    for index in range(1, len(slides) + 1):
        content_types.append(
            f'<Override PartName="/ppt/slides/slide{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
    if active_content:
        content_types.append(
            '<Override PartName="/ppt/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/>'
        )
    content_types.append("</Types>")

    sld_ids = "".join(
        f'<p:sldId id="{255 + index}" r:id="rId{index}"/>'
        for index in range(1, len(slides) + 1)
    )
    presentation = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:sldIdLst>{sld_ids}</p:sldIdLst>
 <p:sldSz cx="12192000" cy="6858000" type="screen16x9"/>
</p:presentation>'''
    presentation_rels = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for index in range(1, len(slides) + 1):
        presentation_rels.append(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{index}.xml"/>'
        )
    presentation_rels.append("</Relationships>")

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr(
            "_rels/.rels",
            '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>''',
        )
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", "".join(presentation_rels))
        image_written = False
        chart_written = False
        for page, objects in enumerate(slides, start=1):
            slide_xml, has_image, has_chart = _slide_xml(objects)
            archive.writestr(f"ppt/slides/slide{page}.xml", slide_xml)
            relationships = []
            if has_image:
                relationships.append(
                    '<Relationship Id="rIdImage1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/>'
                )
                image_written = True
            if has_chart:
                relationships.append(
                    '<Relationship Id="rIdChart1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" '
                    'Target="../charts/chart1.xml"/>'
                )
                chart_written = True
            if external_relationship and page == 1:
                relationships.append(
                    '<Relationship Id="rIdExternal" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/" TargetMode="External"/>'
                )
            if relationships:
                archive.writestr(
                    f"ppt/slides/_rels/slide{page}.xml.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    + "".join(relationships)
                    + "</Relationships>",
                )
        if image_written:
            archive.writestr("ppt/media/image1.png", image_bytes)
        if chart_written:
            archive.writestr(
                "ppt/charts/chart1.xml",
                '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
 <c:chart><c:plotArea><c:barChart><c:ser>
  <c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>销售额</c:v></c:pt></c:strCache></c:strRef></c:tx>
  <c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>2026</c:v></c:pt></c:strCache></c:strRef></c:cat>
  <c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>100</c:v></c:pt></c:numCache></c:numRef></c:val>
 </c:ser></c:barChart></c:plotArea></c:chart>
</c:chartSpace>''',
            )
        if active_content:
            archive.writestr("ppt/vbaProject.bin", b"not executable test content")
    return path


def _slide_xml(objects: Sequence[Mapping[str, object]]) -> tuple[str, bool, bool]:
    fragments = []
    has_image = False
    has_chart = False
    for index, item in enumerate(objects, start=2):
        kind = str(item.get("kind", "text"))
        x = int(item.get("x", 600_000))
        y = int(item.get("y", 300_000 + index * 500_000))
        width = int(item.get("w", 8_000_000))
        height = int(item.get("h", 800_000))
        name = html.escape(str(item.get("name", f"Object {index}")), quote=True)
        alt = html.escape(str(item.get("alt", "")), quote=True)
        hidden_attr = ' hidden="1"' if bool(item.get("hidden", False)) else ""
        if kind == "chart":
            has_chart = True
            fragments.append(
                f'''<p:graphicFrame>
 <p:nvGraphicFramePr><p:cNvPr id="{index}" name="{name}"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
 <p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{width}" cy="{height}"/></p:xfrm>
 <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
  <c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rIdChart1"/>
 </a:graphicData></a:graphic>
</p:graphicFrame>'''
            )
            continue
        if kind == "image":
            has_image = True
            fragments.append(
                f'''<p:pic>
 <p:nvPicPr><p:cNvPr id="{index}" name="{name}" descr="{alt}"{hidden_attr}/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
 <p:blipFill><a:blip r:embed="rIdImage1"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
 <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{width}" cy="{height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
</p:pic>'''
            )
            continue
        text = html.escape(str(item.get("text", "")))
        font_size = int(float(item.get("font_pt", 20)) * 100)
        fragments.append(
            f'''<p:sp>
 <p:nvSpPr><p:cNvPr id="{index}" name="{name}" descr="{alt}"{hidden_attr}/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
 <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{width}" cy="{height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
 <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" sz="{font_size}"><a:latin typeface="Aptos"/></a:rPr><a:t>{text}</a:t></a:r><a:endParaRPr lang="zh-CN"/></a:p></p:txBody>
</p:sp>'''
        )
    xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld><p:spTree>
  <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
  <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
  {''.join(fragments)}
 </p:spTree></p:cSld>
</p:sld>'''
    return xml, has_image, has_chart
