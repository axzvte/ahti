"""Portable XLSX export for the existing per-image PSNR verification records."""
from __future__ import annotations

import csv
import math
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
COLUMNS = (
    ('图片编号', 'image'),
    ('处理前全图 PSNR', 'baseline_global_psnr'),
    ('处理后全图 PSNR', 'processed_global_psnr'),
    ('处理前重要区域 PSNR', 'baseline_roi_psnr'),
    ('处理后重要区域 PSNR', 'processed_roi_psnr'),
)
STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="14"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts>
<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFCE4D6"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="2" fontId="2" fillId="3" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def xml_bytes(element):
    return ET.tostring(element, encoding='utf-8', xml_declaration=True)


def cell(row, reference, value, style=0, formula=None):
    if value is None:
        return
    element = ET.SubElement(row, 'c', r=reference, s=str(style))
    if formula is not None:
        if isinstance(value, str):
            element.set('t', 'str')
        ET.SubElement(element, 'f').text = formula
        ET.SubElement(element, 'v').text = str(value)
    elif isinstance(value, str):
        element.set('t', 'inlineStr')
        ET.SubElement(ET.SubElement(element, 'is'), 't').text = value
    else:
        ET.SubElement(element, 'v').text = format(value, '.17g')


def metric(value):
    if value is None or value == '':
        return None
    value = float(value)
    if math.isnan(value) or value == -math.inf:
        raise ValueError('PSNR report contains an undefined or negative infinite value')
    return '∞' if math.isinf(value) else value


def sheet_name(name, used):
    base = re.sub(r'[\\/*?:\[\]]', '_', name).strip("'") or 'Application'
    base = base[:31]
    candidate = base
    suffix = 1
    while candidate.casefold() in used or candidate.casefold() == 'history':
        suffix += 1
        ending = f'_{suffix}'
        candidate = base[:31-len(ending)] + ending
    used.add(candidate.casefold())
    return candidate


def worksheet(application, records):
    root = ET.Element('worksheet', xmlns=MAIN)
    columns = COLUMNS
    if any('flip_only_global_psnr' in record for record in records):
        columns = (
            *COLUMNS[:2],
            ('仅翻转全图 PSNR', 'flip_only_global_psnr'),
            COLUMNS[2], COLUMNS[3],
            ('仅翻转重要区域 PSNR', 'flip_only_roi_psnr'),
            COLUMNS[4],
        )
    last_column = chr(64+len(columns))
    average_row = len(records)+4
    ET.SubElement(root, 'dimension', ref=f'A1:{last_column}{average_row+2}')
    view = ET.SubElement(ET.SubElement(root, 'sheetViews'), 'sheetView', workbookViewId='0', showGridLines='0')
    ET.SubElement(view, 'pane', ySplit='3', topLeftCell='A4', activePane='bottomLeft', state='frozen')
    ET.SubElement(root, 'sheetFormatPr', defaultRowHeight='20')
    cols = ET.SubElement(root, 'cols')
    ET.SubElement(cols, 'col', min='1', max='1', width='20', customWidth='1')
    ET.SubElement(cols, 'col', min='2', max=str(len(columns)), width='27', customWidth='1')
    data = ET.SubElement(root, 'sheetData')
    row = ET.SubElement(data, 'row', r='1', ht='28', customHeight='1')
    cell(row, 'A1', f'{application} — PSNR 结果', 1)
    row = ET.SubElement(data, 'row', r='2', ht='25', customHeight='1')
    cell(row, 'A2', '单位 dB；各项均以精确电路输出为参考。')
    row = ET.SubElement(data, 'row', r='3', ht='42', customHeight='1')
    for index, (title, _) in enumerate(columns):
        cell(row, f'{chr(65+index)}3', title, 2)
    values = [[] for _ in columns[1:]]
    failed = []
    for number, record in enumerate(records, 4):
        row = ET.SubElement(data, 'row', r=str(number))
        completed = record.get('status') == 'completed'
        cell(row, f'A{number}', str(record['image']), 0 if completed else 5)
        if not completed:
            failed.append(str(record['image']))
            continue
        for index, (_, key) in enumerate(columns[1:], 2):
            value = metric(record.get(key))
            cell(row, f'{chr(64+index)}{number}', value, 3)
            if value is not None:
                values[index-2].append(value)
    row = ET.SubElement(data, 'row', r=str(average_row), ht='24', customHeight='1')
    cell(row, f'A{average_row}', '平均', 4)
    for index, column_values in enumerate(values, 2):
        column = chr(64+index)
        mean = ('∞' if '∞' in column_values else math.fsum(column_values)/len(column_values)
                if column_values else '')
        area = f'{column}4:{column}{average_row-1}'
        formula = f'IF(COUNTIF({area},"∞")>0,"∞",IF(COUNT({area})=0,"",AVERAGE({area})))'
        cell(row, f'{column}{average_row}', mean, 4, formula)
    row = ET.SubElement(data, 'row', r=str(average_row+1), ht='24', customHeight='1')
    cell(row, f'A{average_row+1}', '平均值为各图片 PSNR 的算术平均；∞ 表示与精确参考完全一致。')
    if failed:
        row = ET.SubElement(data, 'row', r=str(average_row+2), ht='24', customHeight='1')
        cell(row, f'A{average_row+2}', '失败图片保留编号、数值留空，平均仅计入完成结果。失败图片：'+', '.join(failed), 5)
    ET.SubElement(root, 'autoFilter', ref=f'A3:{last_column}{average_row-1}')
    merges = ET.SubElement(root, 'mergeCells')
    for reference in (f'A1:{last_column}1', f'A2:{last_column}2', f'A{average_row+1}:{last_column}{average_row+1}'):
        ET.SubElement(merges, 'mergeCell', ref=reference)
    if failed:
        ET.SubElement(merges, 'mergeCell', ref=f'A{average_row+2}:{last_column}{average_row+2}')
    ET.SubElement(root, 'pageMargins', left='0.3', right='0.3', top='0.5', bottom='0.5', header='0.2', footer='0.2')
    return xml_bytes(root)


def export_psnr_workbook(records, path):
    """Write one sheet per application, with numeric data and cached mean formulas."""
    grouped = {}
    for record in records:
        grouped.setdefault(record['application'], []).append(record)
    if not grouped:
        raise ValueError('No verification rows available for Excel export')
    workbook = ET.Element('workbook', xmlns=MAIN, attrib={'xmlns:r': REL})
    sheets = ET.SubElement(workbook, 'sheets')
    relationships = ET.Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    content = ET.Element('Types', xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    ET.SubElement(content, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(content, 'Default', Extension='xml', ContentType='application/xml')
    for part, kind in (('/xl/workbook.xml', 'sheet.main'), ('/xl/styles.xml', 'styles')):
        ET.SubElement(content, 'Override', PartName=part, ContentType=f'application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml')
    parts = {}
    used = set()
    for index, (application, rows) in enumerate(grouped.items(), 1):
        ET.SubElement(sheets, 'sheet', name=sheet_name(application, used), sheetId=str(index), attrib={'r:id': f'rId{index}'})
        ET.SubElement(relationships, 'Relationship', Id=f'rId{index}', Type=REL+'/worksheet', Target=f'worksheets/sheet{index}.xml')
        ET.SubElement(content, 'Override', PartName=f'/xl/worksheets/sheet{index}.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
        parts[f'xl/worksheets/sheet{index}.xml'] = worksheet(application, rows)
    ET.SubElement(relationships, 'Relationship', Id='styles', Type=REL+'/styles', Target='styles.xml')
    ET.SubElement(workbook, 'calcPr', calcId='191029', fullCalcOnLoad='1')
    parts.update({'[Content_Types].xml': xml_bytes(content), 'xl/workbook.xml': xml_bytes(workbook),
                  'xl/_rels/workbook.xml.rels': xml_bytes(relationships), 'xl/styles.xml': STYLES})
    parts['_rels/.rels'] = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                            f'<Relationship Id="officeDocument" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.xlsx.tmp', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as archive:
            for name, document in parts.items():
                archive.writestr(name, document)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def export_results_directory(directory):
    directory = Path(directory)
    with (directory/'images.csv').open(encoding='utf-8-sig', newline='') as stream:
        records = list(csv.DictReader(stream))
    return export_psnr_workbook(records, directory/'psnr_results.xlsx')
