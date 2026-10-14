"""Renderer-level regression; set PLANTUML_JAR to a local supported jar."""
from datetime import date
import importlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

import test_actual_progress_uncertain_schedule as uncertain

OVERLAY = uncertain.OVERLAY


class ProgressRenderingTests(unittest.TestCase):
    def test_shared_header_avoids_renderer_specific_column_commands(self):
        generator = importlib.import_module('sync-quarter-gantt')
        lines = generator.header_lines(Path('/project/quarters/2026-Q4/gantt'),
                                       'Example', date(2026, 10, 1), [], [])
        self.assertFalse(any(line.lower().startswith(('hide column', 'show column', 'label on'))
                             for line in lines))

    @unittest.skipUnless(os.environ.get('PLANTUML_JAR'), 'PLANTUML_JAR is not configured')
    def test_generated_header_keeps_task_and_milestone_labels(self):
        generator = importlib.import_module('sync-quarter-gantt')
        lines = generator.header_lines(Path('/project/quarters/2026-Q4/gantt'),
                                       'Example', date(2026, 10, 1), [], [])
        # Keep the renderer fixture independent of today's calendar date.
        lines = [line for line in lines if 'TODAY_MARK' not in line]
        lines += ['[Wide task] requires 10 days',
                  '[Long name for a short task] requires 1 day',
                  '[Release] happens at 2026/10/12', '@endgantt']
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'labels.puml'
            source.write_text('\n'.join(lines), encoding='utf-8')
            subprocess.run(['java', '-Djava.awt.headless=true', '-jar',
                            os.environ['PLANTUML_JAR'], '-tsvg', str(source)],
                           check=True, capture_output=True, timeout=60)
            root = ET.parse(source.with_suffix('.svg')).getroot()
        texts = [''.join(node.itertext()) for node in root.iter()
                 if node.tag.endswith('}text')]
        for label in ('Wide task', 'Long name for a short task', 'Release'):
            self.assertEqual(texts.count(label), 1)

    @unittest.skipUnless(os.environ.get('PLANTUML_JAR'), 'PLANTUML_JAR is not configured')
    def test_unknown_zero_partial_and_done_have_distinct_fill_semantics(self):
        lines = ['@startgantt', 'Project starts 2026-09-07']
        for index, progress in enumerate((None, 0, 50, 100)):
            task = uncertain.UncertainScheduleTests().task(progress=progress)
            task.task_id = f'CASE{index}'
            task.actual_start = '2026-09-07'
            schedules = OVERLAY.task_schedules({task.task_id: task}, set(), date(2026, 9, 7), OVERLAY.DEFAULT_TEAM_RESOURCES)
            lines.extend(OVERLAY.render_task(task, schedules))
            self.assertEqual(task.progress, progress)
        lines.append('@endgantt')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'progress.puml'
            source.write_text('\n'.join(lines))
            subprocess.run(['java', '-Djava.awt.headless=true', '-jar', os.environ['PLANTUML_JAR'], '-tsvg', str(source)], check=True, capture_output=True, timeout=60)
            root = ET.parse(source.with_suffix('.svg')).getroot()
        groups = {}
        for node in root.iter():
            if node.tag.endswith('}rect') and node.attrib.get('style', '').startswith('stroke:none;'):
                attributes = dict(node.attrib)
                fill = attributes.get('fill', '')
                if fill.startswith('#') and len(fill) == 4:
                    attributes['fill'] = '#' + ''.join(char * 2 for char in fill[1:])
                groups.setdefault(float(attributes['y']), []).append(attributes)
        # Resource histogram bars follow the four task rows.
        rows = [groups[y] for y in sorted(groups)[:4]]
        self.assertEqual(len(rows), 4)
        self.assertEqual([part['fill'] for part in rows[0]], ['none'])
        self.assertEqual([part['fill'] for part in rows[1]], ['#FFFFFF'])
        self.assertEqual([part['fill'] for part in rows[2]], ['#F08080', '#FFFFFF'])
        self.assertAlmostEqual(float(rows[2][0]['width']), float(rows[2][1]['width']))
        self.assertEqual([part['fill'] for part in rows[3]], ['#F08080'])
