#!/usr/bin/env python3
"""Private append-only planning evidence. No scheduler, model, Calendar or task writes."""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

try:
    from . import situation_brief as brief
except ImportError:
    import situation_brief as brief

SCHEMA = 'planning-receipt/v1'
HEX = re.compile(r'^[0-9a-f]{64}$')
ACTIVE = {'open', 'active', 'in_progress', 'in progress', 'doing', 'todo', 'to do',
          'not started', 'planned', 'waiting', 'blocked', 'paused', 'queued'}
NEXT = {None: {'source', 'missed', 'outcome-source'}, 'source': {'baseline', 'missed'},
        'baseline': {'proposal', 'missed'}, 'proposal': {'label'},
        'outcome-source': {'outcome'}, 'label': {'label'}, 'missed': set(), 'outcome': {'outcome'}}


def clock_now():
    return dt.datetime.now(dt.timezone.utc)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def parse(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('nonfinite JSON')
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    # JSON exponent overflow also creates infinity without parse_constant.
    def finite(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError('nonfinite JSON')
        if isinstance(item, dict):
            for child in item.values(): finite(child)
        elif isinstance(item, list):
            for child in item: finite(child)
    finite(value)
    return value


def text(value, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('invalid text')
    return value


def fields(data, required, optional=()):
    if not isinstance(data, dict) or not set(required) <= set(data) or set(data) - set(required) - set(optional):
        raise ValueError('invalid fields')


def instant(value):
    parsed, issue = brief._parse_time(value)
    if issue or parsed is None:
        raise ValueError('aware timestamp required')
    return parsed


def number(value):
    if value is None:
        return None
    try:
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 100000:
            raise ValueError('invalid nonnegative measure')
    except OverflowError:
        raise ValueError('invalid nonnegative measure') from None
    return value


def read(path, limit=2 * 1024 * 1024):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('physical absolute file required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or before.st_size > limit):
            raise ValueError('unsafe or oversized file')
        with os.fdopen(os.dup(fd), 'rb') as f:
            data = f.read(limit + 1)
        after = os.fstat(fd)
        if (len(data) != before.st_size or len(data) > limit
                or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('source changed while reading')
        return data
    finally:
        os.close(fd)


def publish(path, data):
    """Atomic exclusive publication under the case lock; never replace a receipt."""
    temp = path.parent / ('.tmp-' + uuid.uuid4().hex)
    try:
        with temp.open('xb') as f:
            os.chmod(temp, 0o600); f.write(data); f.flush(); os.fsync(f.fileno())
        os.link(temp, path, follow_symlinks=False)  # refuses an existing destination
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked(case):
    case = Path(case)
    if (not case.is_absolute() or case.resolve() != case
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', case.name)):
        raise ValueError('physical case path and safe case name required')
    case.mkdir(parents=True, exist_ok=True, mode=0o700)
    if case.stat().st_uid != os.getuid() or case.stat().st_mode & 0o077:
        raise ValueError('owner-private case directory required')
    fd = os.open(case / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield case
    finally:
        os.close(fd)


def pin(ref, case, name, loading=False, pending=None):
    fields(ref, {'path', 'sha256'})
    if not isinstance(ref['sha256'], str) or not HEX.fullmatch(ref['sha256']):
        raise ValueError('invalid source hash')
    if not isinstance(ref['path'], str) or not Path(ref['path']).is_absolute():
        raise ValueError('absolute source reference required')
    target = case / name
    data = read(target if loading or target.exists() else Path(ref['path']))
    if sha(data) != ref['sha256']:
        raise ValueError('source hash mismatch')
    value = parse(data)
    if not loading and not target.exists():
        if pending is None: raise ValueError('snapshot preparation required')
        pending[target] = data
    return value


def lines(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('bounded text list required')
    return [text(v) for v in value]


def response(value, recorded):
    fields(value, {'task_id', 'turn_id', 'message_id', 'reference_limit', 'observed_at'})
    uuid.UUID(text(value['task_id'],64))
    if not value['turn_id'] and not value['message_id']:
        raise ValueError('an actual answer reference is required')
    for key in ['turn_id', 'message_id']:
        if value[key] is not None: text(value[key], 160)
    text(value['reference_limit'])
    if instant(value['observed_at']) > recorded:
        raise ValueError('answer is in the future')


def modules():
    root = Path(__file__).resolve().parent
    return {name: sha(read(root / name)) for name in ['situation_brief.py', 'work_view.py', 'priority_context.py']}


def eligible_rows(source):
    board = source.get('base', source) if isinstance(source, dict) else None
    if not isinstance(board, dict) or not isinstance(board.get('items'), list) or len(board['items']) > 10000:
        raise ValueError('bounded board items required')
    eligible, exclusions, seen = [], [], set()
    for index, row in enumerate(board['items']):
        if not isinstance(row, dict):
            exclusions.append({'index': index, 'reason': 'invalid_row'}); continue
        identity = brief._identity(row.get('id'))
        if identity is None or identity in seen:
            raise ValueError('missing or duplicate work identity')
        seen.add(identity)
        status = row.get('effective_status', row.get('state'))
        if not isinstance(status, str) or status.lower() not in ACTIVE:
            exclusions.append({'index': index, 'id': identity, 'reason': 'status_not_explicitly_active'}); continue
        one = brief._deadline_baseline({'items': [row]}, True, dt.datetime.now(dt.timezone.utc))
        if not one['items']:
            exclusions.append({'index': index, 'id': identity, 'reason': 'no_valid_explicit_due'}); continue
        eligible.append((index, row))
    return eligible, exclusions


def identifiers(value, universe):
    if (not isinstance(value, list) or len(value) > 3 or any(not isinstance(x, str) for x in value)
            or len(set(value)) != len(value) or not set(value) <= set(universe)):
        raise ValueError('up to three unique eligible identities required')
    return value


def prepare(kind, data, chain, case, at, loading=False, pending=None):
    """Validate both new input and every persisted receipt; derive only deterministic evidence."""
    if kind == 'source':
        fields(data, {'sweep_run_id', 'encounter_id', 'encountered_at', 'frozen_design', 'source_ref',
                      'source_cutoff', 'board_current', 'coverage_limits', 'development_case',
                      'already_ranked_or_labelled'})
        text(data['sweep_run_id'],160); text(data['encounter_id'],160); lines(data['coverage_limits'])
        if data['board_current'] is not True or data['development_case'] is not False or data['already_ranked_or_labelled'] is not False:
            raise ValueError('prospective current held-out source required')
        design = pin(data['frozen_design'], case, 'design-artifact.json', loading, pending)
        if not isinstance(design, dict): raise ValueError('invalid frozen design')
        if not instant(design['frozen_at']) <= instant(data['encountered_at']) <= at:
            raise ValueError('encounter must follow design freeze and precede capture')
        cutoff = instant(data['source_cutoff'])
        if not 0 <= (at - cutoff).total_seconds() <= brief.SOURCE_MAX_AGE_SECONDS:
            raise ValueError('source cutoff is stale or future')
        source = pin(data['source_ref'], case, 'source-artifact.json', loading, pending)
        rows, excluded = eligible_rows(source)
        return {'eligible_indices': [i for i, _ in rows], 'eligible_ids': [r['id'] for _,r in rows],
                'exclusions': excluded, 'baseline_module_hashes': modules()}
    if kind in {'baseline','proposal'} and (at-instant(chain[0]['recorded_at'])).total_seconds()>900:
        raise ValueError('prospective preparation window expired; record missed')
    if kind == 'baseline':
        fields(data, set())
        src = chain[0]; source = pin(src['data']['source_ref'], case, 'source-artifact.json', True)
        rows, _ = eligible_rows(source)
        if modules() != src['derived']['baseline_module_hashes']:
            raise ValueError('baseline release changed')
        result = brief._deadline_baseline({'items': [row for _, row in rows]}, True, at)
        return {'function': '_deadline_baseline', 'module_hashes': modules(), 'result': result,
                'ordered_ids': [r['work_id'] for r in result['items']]}
    if kind == 'proposal':
        fields(data, {'ordered_ids', 'reasons', 'coverage_limits', 'human_labels_known'})
        identifiers(data['ordered_ids'], chain[0]['derived']['eligible_ids'])
        if not data['ordered_ids']: raise ValueError('nonempty prospective proposal required')
        if data['human_labels_known'] is not False: raise ValueError('labels must be unknown at proposal')
        if len(lines(data['reasons'])) != len(data['ordered_ids']): raise ValueError('one reason per proposed item required')
        lines(data['coverage_limits']); return {}
    if kind == 'label':
        fields(data, {'answer_reference', 'human_top_three_ids', 'usefulness', 'correction_burden',
                      'human_supervision_minutes', 'hard_failures', 'raw_answer_minimal'})
        label_values = [data[k] for k in ['human_top_three_ids','usefulness','correction_burden','human_supervision_minutes','hard_failures']]
        if data['answer_reference'] is None:
            if (any(v is not None for v in label_values) or data['raw_answer_minimal'] is not None
                    or any(r['kind']=='label' and r['data']['answer_reference'] is not None for r in chain)):
                raise ValueError('labels require an actual human answer reference')
        else:
            response(data['answer_reference'], at)
            if instant(data['answer_reference']['observed_at']) < instant(chain[2]['recorded_at']):
                raise ValueError('label predates the recorded proposal')
            text(data['raw_answer_minimal'])
        for prior in (r for r in chain if r['kind']=='label'):
            for key in ['human_top_three_ids','usefulness','correction_burden','human_supervision_minutes','hard_failures']:
                if prior['data'][key] is not None and data[key] is None:
                    raise ValueError('known label cannot be replaced by missing data')
            old = prior['data']['answer_reference']
            if old and data['answer_reference'] and instant(data['answer_reference']['observed_at']) < instant(old['observed_at']):
                raise ValueError('answer is older than recorded human evidence')
        human = data['human_top_three_ids']
        if human is not None: identifiers(human, chain[0]['derived']['eligible_ids'])
        if data['usefulness'] is not None and data['usefulness'] not in ['useful','not_useful']:
            raise ValueError('invalid usefulness label')
        number(data['correction_burden']); number(data['human_supervision_minutes'])
        if data['hard_failures'] is not None: lines(data['hard_failures'])
        return {'baseline_agreement': None if human is None else len(set(human) & set(chain[1]['derived']['ordered_ids'])),
                'proposal_agreement': None if human is None else len(set(human) & set(chain[2]['data']['ordered_ids'])),
                'complete_labels': all(v is not None for v in label_values), 'automatic_win': False}
    if kind == 'missed':
        fields(data, {'reason', 'encountered_at', 'evidence_reference'})
        text(data['reason']); text(data['evidence_reference'])
        if data['encountered_at'] is not None and instant(data['encountered_at']) > at:
            raise ValueError('encounter is in the future')
        return {'paired_scoring_eligible': False, 'replacement_case_allowed': False}
    if kind == 'outcome-source':
        fields(data, {'proposal_ref', 'calendar_ref'})
        proposal = pin(data['proposal_ref'], case, 'proposal-artifact.json', loading, pending)
        calendar = pin(data['calendar_ref'], case, 'calendar-artifact.json', loading, pending)
        if (not isinstance(proposal,dict) or not isinstance(calendar,dict)
                or proposal.get('schema') != 'iris-schedule-pilot-proposal/v1'
                or calendar.get('schema') != 'iris-calendar-scheduled-receipt/v1'
                or proposal.get('status') != 'scheduled_outcome_pending'
                or not proposal.get('proposal_id') or not proposal.get('event_id')
                or any(proposal.get(k) != calendar.get(k) for k in ['proposal_id','event_id','start','focus_end','end','approval'])):
            raise ValueError('exact approved proposal and calendar receipt required')
        text(proposal['proposal_id'],160); text(proposal['event_id'],256)
        approval = proposal.get('approval')
        if not isinstance(approval,dict) or not approval.get('quote') or not approval.get('task_id') or not approval.get('turn_id'):
            raise ValueError('explicit original approval required')
        text(approval['quote']); uuid.UUID(text(approval['task_id'],64)); text(approval['turn_id'],160)
        if not instant(proposal['start']) < instant(proposal['focus_end']) <= instant(proposal['end']):
            raise ValueError('invalid block interval')
        estimate=number(proposal['estimated_focus_minutes'])
        if estimate is None or estimate<=0: raise ValueError('positive original estimate required')
        return {'proposal_id':proposal['proposal_id'], 'event_id':proposal['event_id'], 'block_end':proposal['end'],
                'estimated_focus_minutes':proposal['estimated_focus_minutes'], 'completion_inferred':False}
    if kind == 'outcome':
        fields(data, {'outcome', 'focus_minutes', 'source', 'answer_reference', 'raw_answer_minimal'})
        end = instant(chain[0]['derived']['block_end'])
        if at < end: raise ValueError('block outcome is not yet due')
        if data['outcome'] not in ['done','partial','moved','skipped','unknown'] or data['source'] not in ['self_report','unknown']:
            raise ValueError('invalid outcome or evidence source')
        number(data['focus_minutes'])
        if data['source'] == 'unknown':
            if (data['outcome'] != 'unknown' or any(data[k] is not None for k in ['focus_minutes','answer_reference','raw_answer_minimal'])
                    or any(r['kind']=='outcome' and r['data']['source']!='unknown' for r in chain)):
                raise ValueError('unknown outcome must not invent an answer or minutes')
        else:
            response(data['answer_reference'], at); text(data['raw_answer_minimal'])
            if instant(data['answer_reference']['observed_at']) < end: raise ValueError('answer predates block end')
        for prior in (r for r in chain if r['kind']=='outcome'):
            old=prior['data']
            if ((old['outcome']!='unknown' and data['outcome']=='unknown')
                    or (old['focus_minutes'] is not None and data['focus_minutes'] is None)):
                raise ValueError('known outcome cannot be replaced by missing data')
            if old['answer_reference'] and data['answer_reference'] and instant(data['answer_reference']['observed_at']) < instant(old['answer_reference']['observed_at']):
                raise ValueError('answer is older than recorded human evidence')
        return {'completion_inferred':False, 'timing_calibration_eligible':data['outcome']=='done' and data['focus_minutes'] is not None and data['focus_minutes']>0}
    raise ValueError('unsupported operation')


def load(case):
    chain, tail = [], None
    observed_now=clock_now()
    receipts = sorted(case.glob('[0-9]*-*.json'))
    if len(receipts) > 16: raise ValueError('too many receipts')
    for index, path in enumerate(receipts,1):
        raw = read(path,256*1024); row = parse(raw)
        fields(row, {'schema','case_id','sequence','kind','recorded_at','previous_sha256','data','derived'})
        kind = row['kind']
        if (row['schema'] != SCHEMA or row['case_id'] != case.name or type(row['sequence']) is not int
                or row['sequence'] != index or row['previous_sha256'] != tail
                or path.name != f'{index:04d}-{kind}.json' or kind not in NEXT[chain[-1]['kind'] if chain else None]):
            raise ValueError('receipt chain or phase mismatch')
        at = instant(row['recorded_at'])
        if at > observed_now or (chain and at < instant(chain[-1]['recorded_at'])):
            raise ValueError('invalid receipt chronology')
        if prepare(kind,row['data'],chain,case,at,loading=True) != row['derived']:
            raise ValueError('derived receipt evidence changed')
        chain.append(row); tail = sha(raw)
    return chain, tail


def append(case, kind, data, expected_tail):
    with locked(case) as case:
        chain, tail = load(case)
        if len(chain) >= 16: raise ValueError('case receipt bound reached')
        if expected_tail != tail: raise ValueError('case changed; inspect its current tail before retry')
        if kind not in NEXT[chain[-1]['kind'] if chain else None]: raise ValueError('invalid receipt phase')
        # Roundtrip also rejects invalid numeric/JSON inputs supplied by Python callers.
        data = parse(encode(data)); at = clock_now()
        if chain and at < instant(chain[-1]['recorded_at']): raise ValueError('clock regressed; retry later')
        pending={}
        derived = prepare(kind,data,chain,case,at,pending=pending)
        row = {'schema':SCHEMA,'case_id':case.name,'sequence':len(chain)+1,'kind':kind,
               'recorded_at':at.isoformat(),'previous_sha256':tail,'data':data,'derived':derived}
        raw = encode(row)
        if len(raw)>256*1024: raise ValueError('receipt too large')
        for target, artifact in pending.items(): publish(target,artifact)
        path = case / f'{len(chain)+1:04d}-{kind}.json'; publish(path,raw)
        return {'kind':kind,'sequence':len(chain)+1,'tail_sha256':sha(raw),'derived':derived}


def inspect(case):
    with locked(case) as case:
        chain, tail = load(case)
        return {'case_id':case.name,'receipts':len(chain),'phase':chain[-1]['kind'] if chain else None,
                'tail_sha256':tail,'derived':chain[-1]['derived'] if chain else None,'automatic_win':False,
                'label_receipts':sum(r['kind']=='label' for r in chain),
                'outcome_receipts':sum(r['kind']=='outcome' for r in chain)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['inspect','source','baseline','proposal','label','missed','outcome-source','outcome'])
    p.add_argument('--case-dir',required=True,type=Path);p.add_argument('--input',type=Path)
    p.add_argument('--expected-tail',help='Exact last receipt SHA256, or none for an empty case')
    a=p.parse_args(argv)
    try:
        if a.operation=='inspect': result=inspect(a.case_dir)
        else:
            if a.input is None or a.expected_tail is None: raise ValueError('input and expected tail required')
            tail=None if a.expected_tail=='none' else a.expected_tail
            if tail is not None and not HEX.fullmatch(tail): raise ValueError('invalid expected tail')
            result=append(a.case_dir,a.operation,parse(read(a.input,256*1024)),tail)
        print(json.dumps(result,allow_nan=False)); return 0
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError) as error:
        print(json.dumps({'status':'refused','reason':str(error) if isinstance(error,ValueError) else type(error).__name__}));return 2


if __name__=='__main__': raise SystemExit(main())
