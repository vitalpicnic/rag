"""Capture frozen Gemini in an isolated copy; never modify the source corpus.

No cloud calls without --allow-external and GOOGLE_API_KEY. Output contains
private documents/answers: use an operator-only directory outside shared storage.
The operator must arrange an API budget/quota before authorizing a live run.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import zipfile

BASE_COMMIT = 'b2c8022'
UNIT_RUNNER = '''import json, pathlib, sys, unittest
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests'))
counts = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
              skipped=len(result.skipped), expected_failures=len(result.expectedFailures),
              unexpected_successes=len(result.unexpectedSuccesses))
counts['passed'] = counts['run'] - sum(counts[k] for k in counts if k != 'run')
with pathlib.Path(sys.argv[1]).open('x', encoding='utf-8') as out:
    json.dump(counts, out)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def _safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe archive path')
    return path


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _files(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Linked corpus entries are not supported')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _hash(path)
    return result


def _git(checkout, *args):
    executable = shutil.which('git')
    if not executable and os.name == 'nt':
        candidate = Path('C:/Program Files/Git/cmd/git.exe')
        executable = str(candidate) if candidate.exists() else None
    if not executable:
        raise RuntimeError('Git is required')
    return subprocess.check_output(
        [executable, '-c', f'safe.directory={checkout.as_posix()}', '-C', str(checkout), *args],
        stderr=subprocess.PIPE,
    )


def _run(args, cwd, env, log):
    # Logs are private artifacts, never echoed or included in a public error.
    with log.open('xb') as handle:
        subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                       stdout=handle, stderr=subprocess.STDOUT, check=True)


def _save(path, report):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return path


def _run_units(cwd, env, log, report_path):
    _run(['-c', UNIT_RUNNER, str(report_path)], cwd, env, log)


def capture_baseline(checkout: Path, corpus: Path, output: Path, *, allow_external=False) -> Path:
    checkout, corpus, output = (Path(p).resolve() for p in (checkout, corpus, output))
    if output.is_relative_to(corpus) or corpus.is_relative_to(output):
        raise ValueError('Output and corpus must be disjoint')
    if output == checkout or checkout.is_relative_to(output):
        raise ValueError('Output cannot contain the source checkout')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report_path = output / 'capture.json'
    report = {'schema_version': 1, 'status': 'blocked', 'baseline_commit': BASE_COMMIT,
              'started_at': datetime.now(timezone.utc).isoformat(),
              'python': sys.version, 'live_metrics': None}
    if not allow_external:
        report['reason'] = 'Explicit authorization to send corpus to Gemini is required'
        return _save(report_path, report)
    if not os.environ.get('GOOGLE_API_KEY', '').strip():
        report['reason'] = 'GOOGLE_API_KEY is missing'
        return _save(report_path, report)
    if not corpus.is_dir() or not any(p.suffix.lower() in ('.pdf', '.txt') for p in corpus.rglob('*')):
        report['reason'] = 'Corpus with PDF/TXT documents is missing'
        return _save(report_path, report)

    env = os.environ.copy()
    # A baseline must not inherit later RAG configuration or a metrics destination.
    for key in list(env):
        if key.startswith('RAG_') or key in ('GEMINI_API_KEY', 'GOOGLE_GENAI_USE_VERTEXAI'):
            env.pop(key)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    isolated = output / 'baseline'
    try:
        report['baseline_commit'] = _git(checkout, 'rev-parse', BASE_COMMIT).decode().strip()
        before = _files(corpus)
        report['corpus_sha256'] = before
        isolated.mkdir(mode=0o700)
        archive = _git(checkout, 'archive', '--format=zip', BASE_COMMIT)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            for entry in bundle.infolist():
                relative = _safe_member(entry.filename)
                if ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError('Linked source archive entry')
                target = isolated.joinpath(*relative.parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.read(entry))
        # Remove only the tracked placeholder through copy overlay, never touch source.
        shutil.copytree(corpus, isolated / 'data', dirs_exist_ok=True)
        copied = _files(isolated / 'data')
        if any(copied.get(name) != value for name, value in before.items()) or _files(corpus) != before:
            raise ValueError('Corpus changed while copying')
        report['source_sha256'] = {p.relative_to(isolated).as_posix(): _hash(p)
                                   for p in sorted(isolated.rglob('*.py'))}
        cases = isolated / 'tests/fixtures/rag_eval_cases.json'
        report['cases_sha256'] = _hash(cases)
        dataset = json.loads(cases.read_text(encoding='utf-8'))
        topic = dataset['topic']
        if len(PurePosixPath(topic).parts) != 1 or topic in ('.', '..'):
            raise ValueError('Invalid baseline topic')
        _safe_member(topic)
        env['RAG_METRICS_PATH'] = str(output / 'build.metrics.jsonl')
        # Record installed distributions in the actual interpreter including PYTHONPATH.
        _run(['-c', 'import importlib.metadata,json; print(json.dumps(sorted((d.metadata["Name"],d.version) for d in importlib.metadata.distributions())))'],
             isolated, env, output / 'packages.json')
        _run_units(isolated, env, output / 'unit.log', output / 'unit.json')
        _run(['build_index.py', '--topic', topic, '--build', '--output', str(output / 'build.json')],
             isolated, env, output / 'build.log')
        _run(['scripts/evaluate_rag.py', '--run', '--output', str(output / 'results.json')],
             isolated, env, output / 'evaluate.log')
        result = json.loads((output / 'results.json').read_text(encoding='utf-8'))
        if result.get('baseline_status') != 'run' or not result.get('results'):
            raise ValueError('Evaluator did not produce live answers')
        if _files(corpus) != before:
            raise ValueError('Source corpus changed during baseline')
        report.update(status='captured', live_metrics=result.get('summary'),
                      model_options=result.get('model_options'),
                      returned_model_version='unavailable_in_frozen_evaluator',
                      reproducibility='External model alias may change; retain raw A and pair future runs',
                      artifacts_sha256={p.relative_to(isolated).as_posix(): _hash(p)
                                        for p in sorted((isolated / 'faiss_indexes').rglob('*')) if p.is_file()})
    except Exception as exc:
        # Exception messages can contain API keys, prompts, or external response bodies.
        report.update(status='failed', error_type=type(exc).__name__,
                      reason='Baseline did not complete; inspect private artifacts')
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    return _save(report_path, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--corpus', type=Path, required=True, help='Source data directory, containing topic subdirectories')
    parser.add_argument('--output', type=Path, required=True, help='New private directory; never reused')
    parser.add_argument('--allow-external', action='store_true')
    args = parser.parse_args()
    try:
        path = capture_baseline(args.checkout, args.corpus, args.output, allow_external=args.allow_external)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    report = json.loads(path.read_text(encoding='utf-8'))
    print(json.dumps({'status': report['status'], 'report': str(path)}, ensure_ascii=False))
    return 0 if report['status'] == 'captured' else 2


if __name__ == '__main__':
    raise SystemExit(main())
