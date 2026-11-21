"""Reproducible v2 design artifact builder; does not implement a service."""
from pathlib import Path
import base64
import copy
import hashlib
import json
from quant_holding_v3 import holding_contract
from quant_research_v3 import research_contract

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'v2'
OUT.mkdir(exist_ok=True)
def read(name): return json.loads((ROOT / 'v1' / name).read_text(encoding='utf-8'))
def save(name, value): (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
def canon(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
def digest(v): return hashlib.sha256(canon(v)).hexdigest()
def obj(p, optional=()): return {'type':'object','properties':p,'required':[k for k in p if k not in optional],'additionalProperties':False}
def ref(n): return {'$ref':'#/$defs/'+n}
def enum(*v): return {'enum':list(v)}
def nullable(v): return {'anyOf':[v,{'type':'null'}]}
def arr(v, n): return {'type':'array','items':v,'maxItems':n}
def string(n=1000): return {'type':'string','maxLength':n}
ID={'type':'string','minLength':1,'maxLength':64,'pattern':'^[A-Za-z0-9_-]+$'}
INT={'type':'integer','minimum':0,'maximum':9007199254740991}
HASH={'type':'string','pattern':'^[a-f0-9]{64}$'}
BOOL={'type':'boolean'}
T=1794792300000
s=read('business.schema.json')
def upgrade(v):
    if isinstance(v,dict):
        for k,x in v.items():
            if k=='v' and isinstance(x,dict) and x.get('enum')==[1]: x['enum']=[2]
            elif k=='v' and x==1: v[k]=2
            else: upgrade(x)
    elif isinstance(v,list):
        for x in v: upgrade(x)
upgrade(s)
s['$id']='https://tickdesk.invalid/contracts/v2/business.schema.json'
s['title']='TickDesk business protocol 2.0.0 (design)'
s['oneOf']=[v for v in s['oneOf'] if v.get('$ref')!='#/$defs/Chunk']
d=s['$defs'];d.pop('Chunk');d.pop('ImageMarker')

# Manual recovery of non-trade operations preserves the restored projection.
# This decision archives evidence; it never replays a historical write.
decisions = enum('included', 'record', 'discard_intent', 'archive_operation')
d['RecoveryItem']['properties']['allowedDecisions'] = arr(copy.deepcopy(decisions), 4)
d['RecoveryItem']['properties']['disposition'] = nullable(copy.deepcopy(decisions))
d['RecoveryDecision']['properties']['disposition'] = copy.deepcopy(decisions)
d['RecoveryDecision']['allOf'].append({
    'if': {'properties': {'disposition': {'const': 'archive_operation'}}},
    'then': {'properties': {'note': {'type': 'string', 'minLength': 1, 'maxLength': 1000, 'pattern': r'\S'}},
             'not': {'required': ['taskResolution']}}
})
d['Status']['properties']['protocol']['properties']['major']=enum(2)
d['Task']['properties']['score']=nullable(d['Task']['properties']['score'])
# A verified exit carries its own rule evidence instead of an entry ranking score.
# Copying an entry score, inventing zero or deleting this branch would let the
# entry resonance gate stand in for an exit rule.
d['ExitEvidence']=obj({
    'ruleId': {'type':'string','minLength':1,'maxLength':80,'pattern':r'\S'},
    'ruleVersion': dict(INT, minimum=1, maximum=1000),
    'condition': {'type':'string','minLength':1,'maxLength':200,'pattern':r'\S'},
    'decisionClock': enum('completed_daily_bar'),
    'barEndMs': dict(INT, minimum=1), 'barAvailableAtMs': dict(INT, minimum=1),
    'strategyId': {'type':'string','minLength':1,'maxLength':80,'pattern':r'\S'},
    'executable': BOOL,
    'restriction': nullable(enum('t_plus_one','frozen','sellable_unknown','trading_suspended','partially_sellable','strategy_origin_unverified','quote_unavailable','market_closed')),
    'availableQty': nullable(INT),
    'reviewAt': nullable(INT),
}, optional=('availableQty',))
d['Task']['properties']['exit']=nullable(ref('ExitEvidence'))
# Recorded fees stay separate from price and only affect the net result when known.
decimal_amount={'type':'string','pattern':'^(0|[1-9][0-9]*)(\\.[0-9]+)?$','maxLength':48}
d['ExecutionInput']['properties']['fees']=nullable(decimal_amount)
d['cmd_record_execution']['properties']['fees']=nullable(decimal_amount)
d['Execution']['properties']['fees']=nullable(decimal_amount)
d['Execution']['properties']['feeSource']=enum('user','unknown')
d['TradeReviewBasis']=obj({
    'pairing': enum('fifo'), 'version': dict(INT, minimum=1),
    'priceBasis': enum('recorded_execution'), 'feeSource': enum('user_declared'),
    'holdingPeriod': enum('calendar_days_asia_shanghai'), 'asOf': INT,
})
d['TradeReviewPeriod']=obj({
    'from': nullable(INT), 'to': nullable(INT),
    'fromInclusive': BOOL, 'toExclusive': BOOL, 'timezone': enum('Asia/Shanghai'),
})
d['TradeReviewItem']=obj({
    'symbol': ref('Symbol'), 'buyExecutionId': ID, 'sellExecutionId': ID,
    'qty': dict(INT, minimum=1), 'buyPrice': ref('Decimal'), 'sellPrice': ref('Decimal'),
    'grossPnl': ref('Decimal'),
    'buyFee': nullable(ref('Decimal')), 'sellFee': nullable(ref('Decimal')),
    'netPnl': nullable(ref('Decimal')),
    'openedAt': INT, 'closedAt': INT, 'holdingDays': INT,
    'taskId': nullable(ID), 'feeSource': enum('user','unknown'),
})
# A fully closed entry unit, kept separate from per-sell fragments so two
# partial sells of one entry are one hit-rate sample, not two.
d['TradeReviewSample']=obj({
    'entryKey': string(140), 'symbol': ref('Symbol'), 'buyExecutionIds': arr(ID, 50),
    'buyExecutionCount': dict(INT, minimum=1), 'buyExecutionIdsNextCursor': nullable(string(512)),
    'taskId': nullable(ID), 'qty': dict(INT, minimum=1),
    'grossPnl': ref('Decimal'), 'netPnl': nullable(ref('Decimal')),
    'feeSource': enum('user','unknown'), 'openedAt': INT, 'closedAt': INT, 'holdingDays': INT,
})
d['TradeReviewSummary']=obj({
    'closedTrades': INT, 'wins': INT, 'losses': INT, 'flat': INT,
    'netKnownClosedTrades': INT,
    'priceWins': INT, 'priceLosses': INT, 'priceFlat': INT,
    'priceWinRateBasisPoints': nullable(dict(INT, maximum=10000)),
    'openLots': INT, 'openQty': INT,
    'grossPnl': nullable(ref('Decimal')), 'netPnl': nullable(ref('Decimal')),
    'netPnlComplete': BOOL, 'winRateBasisPoints': nullable(dict(INT, maximum=10000)),
    'closedSamples': INT, 'sampleWins': INT, 'sampleLosses': INT, 'sampleFlat': INT,
    'sampleNetKnown': INT,
    'sampleGrossPnl': nullable(ref('Decimal')), 'sampleNetPnl': nullable(ref('Decimal')),
    'sampleNetPnlComplete': BOOL, 'sampleWinRateBasisPoints': nullable(dict(INT, maximum=10000)),
    'excludedCount': INT, 'missingFeeCount': INT,
})
d['TradeReview']=obj({
    'version': enum(1), 'basis': ref('TradeReviewBasis'), 'period': ref('TradeReviewPeriod'),
    'summary': ref('TradeReviewSummary'),
    'items': arr(ref('TradeReviewItem'), 200),
    'exclusions': arr(obj({'executionId': ID, 'reason': enum('superseded','unreconciled','missing_buy_lot')}), 500),
    'samples': arr(ref('TradeReviewSample'), 200),
    'nextCursor': nullable(string(512)), 'hasMore': BOOL,
})
d['Task'].setdefault('allOf', []).append({
    'if': {'properties': {'score': {'type': 'null'}}},
    'then': {'oneOf': [
        {'properties': {
            'direction': {'const': 'risk'}, 'riskEventKey': ID, 'eventRevision': dict(INT, minimum=1),
            'reasons': {'properties': {'verificationStatus': {'const': 'verified'}, 'riskEvidence': {'minItems': 1}}},
        }},
        {'properties': {
            'direction': {'const': 'sell'}, 'exit': ref('ExitEvidence'),
            'reasons': {'properties': {'verificationStatus': {'const': 'verified'}}},
        }, 'required': ['exit']},
    ]},
})
d['RiskEvidence']['properties']['ruleVersion']=string(100)
# A restricted exit has not actually produced a notification yet.
d['Task']['properties']['remindedCount']=dict(INT,minimum=0)
d['Task']['properties']['lastRemindedAt']=nullable(INT)
d['BusinessError']['properties']['details']['properties']['currentVersion']=dict(INT,minimum=0)
d['BusinessError']['properties']['details']['properties']['noteId']=ID
d['cmd_accept_vision_notice']['properties']['accepted']=BOOL
d['cmd_accept_vision_notice']['required'].append('accepted')
d['result_accept_vision_notice']['properties']['consentedAt']=nullable(INT)
queues=d['Status']['properties']['queues'];queues['properties']['fileCleanupPending']=queues['properties'].pop('cleanupPending');queues['required']=[('fileCleanupPending' if k=='cleanupPending' else k) for k in queues['required']]
d['File']=obj({'fileId':ID,'version':INT,'name':string(255),'mime':enum('image/png','image/jpeg'),'sizeBytes':{'type':'integer','minimum':1,'maximum':20971520},'sha256':HASH,'status':enum('available','deleting','deleted','missing'),'createdAt':INT,'deletedAt':nullable(INT)})
d['Upload']=obj({'uploadId':ID,'fileId':ID,'sizeBytes':{'type':'integer','minimum':1,'maximum':20971520},'chunkSize':enum(32768),'totalChunks':{'type':'integer','minimum':1,'maximum':640},'receivedChunks':dict(arr({'type':'integer','minimum':0,'maximum':639},640),uniqueItems=True),'expiresAt':INT,'status':enum('receiving','complete','expired')})
d['Message']={'oneOf':[obj({'messageId':ID,'requestId':ID,'role':enum('user'),'text':string(8000),'at':INT,'status':enum('complete')}),obj({'messageId':ID,'requestId':ID,'role':enum('assistant'),'reply':ref('Reply'),'at':INT})]}
d['Import']['properties']['status']['enum'].append('cancelled')
d['Upload']['properties']['status']['enum'].append('cancelled')
for definition in [d['Import']]:
    definition['properties'].pop('cleanupStatus');definition['required'].remove('cleanupStatus')
    definition['properties'].update(fileId=ID,fileStatus=enum('available','deleting','deleted','missing'))
    definition['required']+=['fileId','fileStatus']
d['cmd_register_image']=obj({'cmd':enum('register_image'),'clientMsgId':ID,'serverEpoch':ID,'importId':ID,'fileId':ID,'autoApply':BOOL},('autoApply',))
d['result_register_image']=obj({'importId':ID,'fileId':ID,'importStatus':enum('pending'),'snapshotSeq':INT})
catalog=read('commands.json');catalog['contractVersion']='2.0.0'
d['ExecutionProgress']=obj({
    'taskId':ID,'symbol':ref('Symbol'),'version':INT,'state':enum('not_started','processing','partially_filled','ended','unknown'),
    'plannedQty':dict(INT,minimum=1),'confirmedFillQty':nullable(INT),'reservedQty':INT,
    'reportedRemainingQty':nullable(INT),'reportedAt':nullable(INT),'reconciliationRequired':BOOL,
})
d['ReportHeader']=obj({
    'reportId':ID,'symbols':arr(ref('Symbol'),20),'asOf':INT,'generatedAt':INT,'validUntil':INT,
    'state':enum('current','historical','expired'),'stance':enum('watch','insufficient','restricted','positive','negative','mixed'),
    'summary':string(550),'origin':enum('manual','automatic'),
    'coverage':obj({key:enum('unavailable','partial','available') for key in ('business','quant','external')}),
})
d['AnalysisReportEvent']=obj({'v':enum(2),'kind':enum('analysis_report'),'id':ID,'dedupeKey':string(200),
    'ts':INT,'serverEpoch':ID,'snapshotSeq':INT,
    'data':obj({'header':ref('ReportHeader'),'notifyUntil':nullable(INT)})})
s['oneOf'].append(ref('AnalysisReportEvent'))
new={
 'begin_upload':({'name':string(255),'mime':enum('image/png','image/jpeg'),'sizeBytes':{'type':'integer','minimum':1,'maximum':20971520},'sha256':HASH},{'upload':ref('Upload')},'write',()),
 'get_upload':({'uploadId':ID},{'upload':ref('Upload')},'read',()),
 'finalize_upload':({'uploadId':ID},{'file':ref('File')},'write',()),
 'list_files':({'cursor':string(512),'limit':{'type':'integer','minimum':1,'maximum':20}},{'items':arr(ref('File'),20),'nextCursor':nullable(string(512))},'read',('cursor','limit')),
 'read_file':({'fileId':ID},{'downloadId':ID,'file':ref('File'),'chunkSize':enum(32768),'totalChunks':{'type':'integer','minimum':1,'maximum':640},'expiresAt':INT},'read',()),
 'delete_file':({'fileId':ID,'baseVersion':INT,'confirm':enum(True)},{'file':ref('File')},'write',()),
 'get_messages':({'cursor':string(512),'limit':{'type':'integer','minimum':1,'maximum':20}},{'items':arr(ref('Message'),20),'nextCursor':nullable(string(512))},'read',('cursor','limit')),
 'list_reports':({'symbol':ref('Symbol'),'cursor':string(512),'limit':dict(INT,minimum=1,maximum=20),'subscribe':BOOL},
    {'items':arr(ref('ReportHeader'),20),'nextCursor':nullable(string(512))},'read',('symbol','cursor','limit','subscribe')),
 'get_report':({'reportId':ID},{'header':ref('ReportHeader'),'text':string(24000),'sources':arr(ref('Source'),20)},'read',()),
 'get_reply_page':({'requestId':ID,'offset':INT,'sha256':HASH},
    {'requestId':ID,'offset':INT,'nextOffset':nullable(INT),'totalChars':dict(INT,minimum=1),
     'sha256':HASH,'text':{'type':'string','minLength':1,'maxLength':2048},'sources':arr(ref('Source'),20)},'read',('offset','sha256')),
 'get_command_status':({'originalEpoch':ID,'targetClientMsgId':ID,'targetRequestHash':HASH},{'state':enum('found','processing','quarantined','not_found','hash_mismatch'),'ackEventId':nullable(ID)},'read',()),
 'cancel_import':({'importId':ID},{'importId':ID,'status':enum('cancelled','already_terminal')},'write',()),
 'cancel_transfer':({'transferId':ID,'kind':enum('upload','download')},{'transferId':ID,'status':enum('cancelled','already_complete')},'write',()),
 'get_execution_progress':({'taskId':ID},{'version':enum(3),'progress':nullable(ref('ExecutionProgress'))},'read',('taskId',)),
 'set_execution_progress':({'taskId':ID,'baseVersion':INT,'state':enum('processing','partially_filled','ended','unknown'),
    'reportedRemainingQty':nullable(INT),'expectedConfirmedQty':nullable(INT),'asOf':INT},
    {'version':enum(3),'progress':ref('ExecutionProgress')},'write',()),
  'get_trade_review':({'symbol':ref('Symbol'),'from':INT,'to':INT,'limit':dict(INT,minimum=1,maximum=200),
      'cursor':string(512),'view':enum('items','samples','exclusions'),
      'sampleEntryKey':string(140),'sampleCursor':string(512)},
     {'version':enum(1),'basis':ref('TradeReviewBasis'),'period':ref('TradeReviewPeriod'),
     'summary':ref('TradeReviewSummary'),
     'items':arr(ref('TradeReviewItem'),200),
     'exclusions':arr(obj({'executionId':ID,'reason':enum('superseded','unreconciled','missing_buy_lot')}),500),
     'samples':arr(ref('TradeReviewSample'),200),
     'nextCursor':nullable(string(512)),'hasMore':BOOL},
    'read', ('symbol','from','to','limit','cursor','view','sampleEntryKey','sampleCursor')),
}
d['TradeReviewNote']=obj({
    'noteId': ID, 'subjectType': enum('execution','sample','review'),
    'subjectId': string(140),
    'verdict': enum('false_positive','missed_opportunity','risk_confirmed','risk_rejected','evidence_insufficient'),
    'note': string(1000), 'reviewer': string(80),
    'evidenceExecutionIds': arr(ID, 20),
    'status': enum('active','superseded','withdrawn'),
    'version': dict(INT, minimum=1), 'supersedesNoteId': nullable(ID),
    'createdAt': INT, 'updatedAt': INT,
})
new['record_trade_review_note']=({'noteId': ID, 'subjectType': enum('execution','sample','review'),
    'subjectId': string(140),
    'verdict': enum('false_positive','missed_opportunity','risk_confirmed','risk_rejected','evidence_insufficient'),
    'note': string(1000), 'reviewer': string(80),
    'evidenceExecutionIds': arr(ID, 20), 'at': INT, 'supersedesNoteId': ID,
}, {'note': ref('TradeReviewNote')}, 'write', ('supersedesNoteId',))
new['get_trade_review_notes']=({'subjectId': string(140), 'cursor': string(512),
    'limit': dict(INT, minimum=1, maximum=50)},
    {'items': arr(ref('TradeReviewNote'), 50), 'nextCursor': nullable(string(512)),
     'hasMore': BOOL, 'manualCount': INT}, 'read', ('subjectId','cursor','limit'))
new['withdraw_trade_review_note']=({'noteId': ID, 'baseVersion': dict(INT, minimum=1),
    'reason': string(500), 'at': INT}, {'note': ref('TradeReviewNote')}, 'write', ())
holding_new, holding_samples, holding_negative = holding_contract(d,T)
new.update(holding_new)
research_new, research_samples, research_negative = research_contract(d, T)
new.update(research_new)
# Display-only addition is scoped to adjust_position, after holding_contract has
# copied its pre-existing fields for the separate reconciliation command.
d['cmd_adjust_position']['properties']['name']={'type':'string','minLength':1,'maxLength':100,'pattern':r'\S'}
for key in ('cmd_get_chart', 'result_get_chart'):
    d[key]['properties']['timeframe'] = enum('1d', '1w', '1mo', '1m', '5m')
    if 'dataStatus' in d[key]['properties']:
        d[key]['properties']['dataStatus']['enum'].append('partial')
d['cmd_get_chart']['properties']['cursor'] = string(512)
d['result_get_chart']['properties']['nextCursor'] = nullable(string(512))
for field, schema in {
    'startAt': INT, 'endMs': INT, 'availableAtMs': nullable(INT),
    'complete': BOOL, 'interval': enum('1m', '5m'),
    'quality': enum('ok', 'duplicate_minute', 'incomplete_group', 'incomplete_amount',
                    'session_gap', 'unknown'),
}.items():
    d['Candle']['properties'][field] = copy.deepcopy(schema)
# A five-minute window without its first minute has no known opening price.
# Expose the gap instead of borrowing the next minute's price as the open.
d['ChartGap']=obj({
    'startAt': INT, 'endMs': INT,
    'quality': enum('missing_first_minute', 'incomplete_group', 'duplicate_minute'),
    'missingMinutes': arr(INT, 5), 'observedMinutes': dict(INT, minimum=1, maximum=5),
})
d['result_get_chart']['properties']['gaps']=arr(ref('ChartGap'), 200)
d['QuoteStatus'] = obj({
    'symbol':ref('Symbol'), 'price':nullable(ref('Decimal')), 'asOf':nullable(INT),
    'source':nullable(ref('Source')), 'timeBasis':nullable(enum('snapshot','trade','settlement')),
    'quality':enum('fresh','stale','missing'),
    'delivery':enum('available','missing','rate_limited','offline','unavailable','unknown'),
    'tradingStatus':enum('suspended','trading','unknown'),
})
new['get_quote_status'] = (
    {'symbols':dict(arr(ref('Symbol'), 20), minItems=1, uniqueItems=True)},
    {'session':enum('open','closed','unknown'), 'items':arr(ref('QuoteStatus'), 20)}, 'read', (),
)
new['get_storage_status'] = (
    {},
    {'status':enum('available','unknown'), 'reason':nullable(string(200)),
     'originalsBytes':nullable(INT), 'quarantineBytes':nullable(INT),
     'stagingBytes':nullable(INT), 'availableBytes':nullable(INT),
     'lastSuccessfulBackupAt':nullable(INT)},
    'read', (),
)
date_text = {'type': 'string', 'format': 'date'}
d['HistoryCoverageAttribution'] = obj({
    'status': enum('all_attributed', 'partial'),
    'window': obj({'start': date_text, 'end': date_text, 'sessions': INT}),
    'missingCells': INT, 'attributedCells': INT, 'unexplainedCells': INT,
    'sources': obj({'sseDays': INT, 'szseDays': INT, 'archiveDays': INT}),
    'archiveSignature': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
    'unexplained': arr(obj({'day': date_text,
                           'symbol': {'type': 'string', 'pattern': '^[0-9]{6}\\.(?:SH|SZ)$'}}), 1000),
})
d['HistoryCoverage'] = obj({
    'source': {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9_-]{0,63}$'},
    'start': date_text, 'through': date_text, 'expectedThrough': date_text, 'updatedAt': INT,
    'rows': INT, 'observedSymbols': INT, 'requestedSymbols': INT, 'conflictingRows': INT,
    'latestDay': obj({'observed': INT, 'expected': INT}),
    'attribution': nullable(ref('HistoryCoverageAttribution')),
}, optional=('attribution',))
new['get_history_coverage'] = ({}, {
    'status': enum('ready', 'partial', 'unavailable', 'disabled'), 'checkedAt': nullable(INT),
    'coverage': nullable(ref('HistoryCoverage')),
    'reason': nullable(enum('history_pending', 'source_unavailable', 'snapshot_invalid', 'refresh_failed', 'provider_pending')),
}, 'read', ())
for name,(args,result,mode,optional) in new.items():
    d['cmd_'+name]=obj({'cmd':enum(name),'clientMsgId':ID,'serverEpoch':nullable(ID) if mode=='read' else ID,**args},optional)
    d['result_'+name]=obj(result)
    d['CommandData']['oneOf'].append(ref('cmd_'+name))
    ack=copy.deepcopy(d['Ack']['oneOf'][0]);ack['properties']['cmd']=enum(name);ack['properties']['result']=ref('result_'+name)
    d['Ack']['oneOf'].insert(-1,ack)
    catalog['commands'].append({'name':name,'mode':mode,'allowedDuringRecovery':mode=='read' and name not in ['read_file','get_upload','get_quote_status','get_reply_page'],'onlyDuringRecovery':False,'request':'#/$defs/cmd_'+name,'result':'#/$defs/result_'+name,'ackTimeoutMs':15000,'completionEvent':None})
# Extend failure ack command discriminator, including nested evidence envelopes.
for a in d['Ack']['oneOf']:
    if a.get('properties',{}).get('ok',{}).get('enum')==[False] and 'enum' in a['properties']['cmd']: a['properties']['cmd']['enum']+=list(new)
save('business.schema.json',s);save('commands.json',catalog)
errors=read('errors.json');errors['contractVersion']='2.0.0'
errors['errors']=[x for x in errors['errors'] if not x['code'].startswith(('IM_','CLEANUP_')) and x['code']!='IMAGE_CLEANUP_FAILED']
for code,meaning in {'FILE_NOT_FOUND':'文件不存在或缺失','FILE_BUSY':'识别或下载仍持有读取租约','FILE_DELETED':'文件已删除，不可重识别','FILE_VERSION_CONFLICT':'删除使用的版本已变化','UPLOAD_EXPIRED':'临时上传过期，使用新上传意图','UPLOAD_INCOMPLETE':'块未齐，不得完成','FILE_HASH_MISMATCH':'块或完整文件摘要不匹配','FILE_TYPE_UNSUPPORTED':'实际 MIME 或解码尺寸不支持','DISK_FULL':'持久存储容量不足','COMMAND_NOT_FOUND':'未查到命令不是未执行证明','PROGRESS_VERSION_CONFLICT':'执行进度 baseVersion 已过期；读取新版本后再提交','TRADE_REVIEW_CURSOR_EXPIRED':'复盘数据已变化或游标过期，请重新查询第一页','TRADE_REVIEW_NOTE_CONFLICT':'人工复核记录已被修改或撤回','TRADE_REVIEW_NOTE_DUPLICATE':'同一事项已有相同结论的复核记录','TRADE_REVIEW_NOTE_NOT_FOUND':'人工复核记录不存在','CHART_CURSOR_EXPIRED':'分钟K线内容或查询已变化，请重新读取第一页'}.items():
    errors['errors'].append({'code':code,'meaning':meaning,'automaticBusinessRetry':False})
errors['errors'].append({'code':'IMPORT_CONTENT_INVALID','meaning':'截图内容无法完整且唯一地转换为合法持仓，整批未应用','automaticBusinessRetry':False})
errors['errors'].append({'code':'VISION_SYMBOLS_UNAVAILABLE','meaning':'识别所需证券主数据不可用，持仓未变更','automaticBusinessRetry':False})
errors['errors'].append({'code':'AI_REPORT_UNAVAILABLE','meaning':'报告不存在或当前配对不可读取','automaticBusinessRetry':False})
errors['errors'].append({'code':'AI_REPLY_UNAVAILABLE','meaning':'回复未成功完成或当前配对端不可读取完整正文','automaticBusinessRetry':False})
save('errors.json',errors)
f=read('fixtures.json');f['contractVersion']='2.0.0';upgrade(f)
def migrate(v):
    if isinstance(v,dict):
        if v.get('noticeVersion')=='vision-v1':v['noticeVersion']='vision-v2'
        if v.get('cmd')=='accept_vision_notice' and 'noticeVersion' in v:v['accepted']=True
        if 'protocol' in v and isinstance(v['protocol'],dict):v['protocol']['major']=2
        if 'cleanupPending' in v:v['fileCleanupPending']=v.pop('cleanupPending')
        if 'cleanupStatus' in v: v.pop('cleanupStatus');v.update(fileId='file_demo',fileStatus='available')
        if 'cleanupRegistered' in v: v.pop('cleanupRegistered');v['fileId']='file_demo'
        if v.get('cmd')=='register_image' and 'category' in v:
            for k in ['category','date','filename','uploadedAt']:v.pop(k,None)
            v['fileId']='file_demo'
        for x in v.values():migrate(x)
    elif isinstance(v,list):
        for x in v:migrate(x)
migrate(f)
ev=read('evidence-vectors.json');upgrade(ev);migrate(ev)
for record in ev['records']:
    raw=canon(record['payload']);record['payloadHash']=hashlib.sha256(raw).hexdigest();record['parts']=[base64.b64encode(raw[i:i+4096]).decode() for i in range(0,len(raw),4096)]
save('evidence-vectors.json',ev)
for c in f['cases']:
    p=c['payload']
    if c['name']=='request_recovery_evidence':
        raw=canon(next(x['payload'] for x in f['cases'] if x['name']=='request_record_execution'))
        p['data']['bytesBase64']=base64.b64encode(raw).decode();p['data']['payloadHash']=hashlib.sha256(raw).hexdigest()
# Obsolete cloud chunk/image-marker cases are historical v1 cases.
f['cases']=[c for c in f['cases'] if c['payload'].get('kind')!='chunk' and c.get('definition') not in ['ImageMarker','Chunk']]
requests={c['payload']['data']['clientMsgId']:c['payload']['data'] for c in f['cases'] if c['valid'] and c['payload'].get('kind')=='command'}
for c in f['cases']:
    a=c['payload']
    if a.get('kind')=='ack' and a['data'].get('clientMsgId') in requests:a['data']['requestHash']=digest(requests[a['data']['clientMsgId']])
    if a.get('kind')=='state_snapshot':
        m=a['data'].get('manifest')
        if m: pass # Rebuilt below after all pages have been migrated.
file={'fileId':'file_demo','version':1,'name':'positions.png','mime':'image/png','sizeBytes':3,'sha256':hashlib.sha256(b'abc').hexdigest(),'status':'available','createdAt':T,'deletedAt':None}
upload={'uploadId':'upload_demo','fileId':'file_demo','sizeBytes':3,'chunkSize':32768,'totalChunks':1,'receivedChunks':[],'expiresAt':T+86400000,'status':'receiving'}
report={'reportId':'report_demo','symbols':['600000.SH'],'asOf':T,'generatedAt':T+1000,'validUntil':T+1800000,
        'state':'current','stance':'insufficient','summary':'尚缺公司披露和量化资料。','origin':'manual',
        'coverage':{'business':'partial','quant':'unavailable','external':'unavailable'}}
samples={
 'begin_upload':({'name':'positions.png','mime':'image/png','sizeBytes':3,'sha256':file['sha256']},{'upload':upload}),
 'get_upload':({'uploadId':'upload_demo'},{'upload':upload}),
 'finalize_upload':({'uploadId':'upload_demo'},{'file':file}),
 'list_files':({}, {'items':[file],'nextCursor':None}),
 'read_file':({'fileId':'file_demo'},{'downloadId':'download_demo','file':file,'chunkSize':32768,'totalChunks':1,'expiresAt':T+86400000}),
 'delete_file':({'fileId':'file_demo','baseVersion':1,'confirm':True},{'file':dict(file,status='deleted',version=2,deletedAt=T)}),
 'get_messages':({}, {'items':[{'messageId':'message_demo','requestId':'client_chat','role':'user','text':'请解释这条风险提示','at':T,'status':'complete'}],'nextCursor':None}),
 'list_reports':({'symbol':'600000.SH','limit':10,'subscribe':True},{'items':[report],'nextCursor':None}),
 'get_report':({'reportId':report['reportId']},{'header':report,'text':report['summary'],'sources':[]}),
 'get_reply_page':({'requestId':report['reportId']},{'requestId':report['reportId'],'offset':0,'nextOffset':None,
    'totalChars':len(report['summary']),'sha256':hashlib.sha256(report['summary'].encode('utf-8')).hexdigest(),
    'text':report['summary'],'sources':[]}),
 'get_command_status':({'originalEpoch':'epoch_demo_a','targetClientMsgId':'client_act','targetRequestHash':digest(requests['client_act'])},{'state':'found','ackEventId':'ack_act'}),
 'cancel_import':({'importId':'import_demo'},{'importId':'import_demo','status':'cancelled'}),
 'cancel_transfer':({'transferId':'download_demo','kind':'download'},{'transferId':'download_demo','status':'cancelled'}),
 'get_quote_status':({'symbols':['600000.SH']}, {'session':'closed','items':[
    {'symbol':'600000.SH','price':'10.78','asOf':T,'source':{'name':'fixture','ref':'fixture:stock-snapshot','asOf':T},
     'timeBasis':'snapshot','quality':'stale','delivery':'available','tradingStatus':'unknown'}]}),
 'get_storage_status':({}, {'status':'available','reason':None,'originalsBytes':0,
    'quarantineBytes':0,'stagingBytes':0,'availableBytes':1_000_000,'lastSuccessfulBackupAt':None}),
 'get_history_coverage':({}, {'status':'partial','checkedAt':T,'reason':None,'coverage':{
    'source':'fuyao','start':'2026-01-20','through':'2026-11-13','expectedThrough':'2026-11-13',
    'updatedAt':T,'rows':900000,'observedSymbols':4600,'requestedSymbols':4605,'conflictingRows':0,
    'latestDay':{'observed':4600,'expected':4605},
    'attribution':{'status':'all_attributed','window':{'start':'2026-01-20','end':'2026-11-13','sessions':120},
    'missingCells':5,'attributedCells':5,'unexplainedCells':0,
    'sources':{'sseDays':120,'szseDays':118,'archiveDays':121},
    'archiveSignature':'a'*64,'unexplained':[]}}}),
 'get_execution_progress':({}, {'version':3,'progress':None}),
 'get_trade_review':({}, {'version':1,'basis':{'pairing':'fifo','version':1,'priceBasis':'recorded_execution',
    'feeSource':'user_declared','holdingPeriod':'calendar_days_asia_shanghai','asOf':T},
    'period':{'from':None,'to':None,'fromInclusive':True,'toExclusive':True,'timezone':'Asia/Shanghai'},
    'summary':{'closedTrades':0,'wins':0,'losses':0,'flat':0,'netKnownClosedTrades':0,
    'priceWins':0,'priceLosses':0,'priceFlat':0,'priceWinRateBasisPoints':None,
    'openLots':0,'openQty':0,'grossPnl':None,
    'netPnl':None,'netPnlComplete':True,'winRateBasisPoints':None,
    'closedSamples':0,'sampleWins':0,'sampleLosses':0,'sampleFlat':0,'sampleNetKnown':0,
    'sampleGrossPnl':None,'sampleNetPnl':None,'sampleNetPnlComplete':True,
    'sampleWinRateBasisPoints':None,'excludedCount':0,'missingFeeCount':0},
    'items':[],'exclusions':[],'samples':[],'nextCursor':None,'hasMore':False}),
 'set_execution_progress':({'taskId':'task_demo','baseVersion':0,'state':'ended','reportedRemainingQty':0,
    'expectedConfirmedQty':300,'asOf':T},{'version':3,'progress':{'taskId':'task_demo','symbol':'300502.SZ',
    'version':1,'state':'ended','plannedQty':1000,'confirmedFillQty':300,'reservedQty':0,
    'reportedRemainingQty':0,'reportedAt':T,'reconciliationRequired':False}}),
 'record_trade_review_note':({'noteId':'review_note_demo','subjectType':'execution',
    'subjectId':'exec_demo','verdict':'false_positive','note':'人工复核：该笔并非计划内信号。',
    'reviewer':'local_user','evidenceExecutionIds':['exec_demo'],'at':T},
    {'note':{'noteId':'review_note_demo','subjectType':'execution','subjectId':'exec_demo',
    'verdict':'false_positive','note':'人工复核：该笔并非计划内信号。','reviewer':'local_user',
    'evidenceExecutionIds':['exec_demo'],'status':'active','version':1,'supersedesNoteId':None,
    'createdAt':T,'updatedAt':T}}),
 'get_trade_review_notes':({'subjectId':'exec_demo'}, {'items':[{'noteId':'review_note_demo',
    'subjectType':'execution','subjectId':'exec_demo','verdict':'false_positive',
    'note':'人工复核：该笔并非计划内信号。','reviewer':'local_user',
    'evidenceExecutionIds':['exec_demo'],'status':'active','version':1,'supersedesNoteId':None,
    'createdAt':T,'updatedAt':T}],'nextCursor':None,'hasMore':False,'manualCount':1}),
 'withdraw_trade_review_note':({'noteId':'review_note_demo','baseVersion':1,'reason':'复核记录作废','at':T},
    {'note':{'noteId':'review_note_demo','subjectType':'execution','subjectId':'exec_demo',
    'verdict':'false_positive','note':'人工复核：该笔并非计划内信号。','reviewer':'local_user',
    'evidenceExecutionIds':['exec_demo'],'status':'withdrawn','version':2,'supersedesNoteId':None,
    'createdAt':T,'updatedAt':T}}),
}
samples.update(holding_samples)
samples.update(research_samples)
for name,(args,result) in samples.items():
    data={'cmd':name,'clientMsgId':'client_'+name,'serverEpoch':'epoch_demo_a',**args}
    req={'v':2,'kind':'command','id':'event_'+name,'dedupeKey':'command:client_'+name,'ts':T,'serverEpoch':'epoch_demo_a','data':data}
    ack={'v':2,'kind':'ack','id':'ack_'+name,'dedupeKey':'ack:client_'+name,'ts':T,'serverEpoch':'epoch_demo_a','snapshotSeq':42,'data':{'refId':req['id'],'clientMsgId':data['clientMsgId'],'requestHash':digest(data),'cmd':name,'ok':True,'result':result,'error':None}}
    f['cases'] += [{'name':'request_'+name,'valid':True,'payload':req},{'name':'ack_'+name,'valid':True,'payload':ack}]
by_name = {c['name']:c['payload'] for c in f['cases']}
for timeframe in ('1w', '1mo'):
    req = copy.deepcopy(by_name['request_get_chart'])
    ack = copy.deepcopy(by_name['ack_get_chart'])
    req['id'] += '_' + timeframe
    req['data']['clientMsgId'] += '_' + timeframe
    req['dedupeKey'] += '_' + timeframe
    req['data']['timeframe'] = timeframe
    ack['id'] += '_' + timeframe
    ack['dedupeKey'] += '_' + timeframe
    ack['data'].update(refId=req['id'], clientMsgId=req['data']['clientMsgId'], requestHash=digest(req['data']))
    ack['data']['result']['timeframe'] = timeframe
    f['cases'] += [{'name':'request_chart_'+timeframe,'valid':True,'payload':req},
                   {'name':'ack_chart_'+timeframe,'valid':True,'payload':ack}]
report_event={'v':2,'kind':'analysis_report','id':'event_analysis_report','dedupeKey':'report:report_demo',
    'ts':T+1000,'serverEpoch':'epoch_demo_a','snapshotSeq':43,
    'data':{'header':dict(report,origin='automatic'),'notifyUntil':None}}
f['cases'].append({'name':'analysis_report_publication','valid':True,'payload':report_event})
p=copy.deepcopy(report_event);p['data']['notifyUntil']='unknown'
f['cases'].append({'name':'analysis_report_rejects_non_numeric_notification_deadline','valid':False,'payload':p})
# Research reads expose availability and typed tri-state values, never internal evidence.
research_ack = next(c['payload'] for c in f['cases'] if c['name']=='ack_get_quant_observations')
for status in ['missing', 'failed']:
    p=copy.deepcopy(research_ack)
    p['data']['result'].update(status=status, scanId=None if status=='missing' else 'failed_scan',
        decisionCutoffMs=None, error='DATA_UNAVAILABLE' if status=='failed' else None, observations=[])
    f['cases'].append({'name':'quant_read_'+status,'valid':True,'payload':p})
for name, edit in [
    ('quant_read_rejects_evidence', lambda result: result['observations'][0].update(evidence={})),
    ('quant_read_unknown_requires_reason', lambda result: result['observations'][0]['entry'].update(reason=None)),
    ('quant_read_false_has_no_missing_reason', lambda result: result['observations'][0]['exit'].update(reason='missing')),
    ('quant_read_response_limit', lambda result: result.update(observations=result['observations'] * 21)),
]:
    p=copy.deepcopy(research_ack);edit(p['data']['result'])
    f['cases'].append({'name':name,'valid':False,'payload':p})
for name, source, fields in holding_negative + research_negative + [('invalid_progress_quantity','set_execution_progress',{'reportedRemainingQty':9007199254740992}),
    ('unexpected_progress_property','set_execution_progress',{'orderStatus':'filled'})]:
    p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='request_'+source));p['data'].update(fields)
    f['cases'].append({'name':name,'valid':False,'payload':p})
# A sell task without an entry score is legal only with verified exit evidence.
exit_ack = copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='ack_get_task'))
task = exit_ack['data']['result']['task']
task['reasons']['verificationStatus']='verified'
task.update(score=None, direction='sell', exit={
    'ruleId':'matrix_daily_exit','ruleVersion':1,'condition':'已批准日线策略的独立退出条件成立',
    'decisionClock':'completed_daily_bar','barEndMs':T-60_000,'barAvailableAtMs':T-30_000,
    'strategyId':'fixture-swing','executable':False,'restriction':'t_plus_one',
    'availableQty':0,'reviewAt':T+86_400_000})
f['cases'].append({'name':'exit_task_with_verified_rule','valid':True,'payload':exit_ack})
p=copy.deepcopy(exit_ack);p['data']['result']['task'].pop('exit')
f['cases'].append({'name':'exit_task_requires_rule_evidence','valid':False,'payload':p})
p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='ack_get_trade_review'))
p['data']['result']['summary']['winRateBasisPoints']=10001
f['cases'].append({'name':'trade_review_rejects_invalid_win_rate','valid':False,'payload':p})
p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='request_record_execution'))
p['data']['fees']='-1'
f['cases'].append({'name':'execution_rejects_negative_fees','valid':False,'payload':p})
for name,mutate in [('negative_file_size',lambda p:p['data'].update(sizeBytes=20971521)),('negative_cloud_file_path',lambda p:p['data'].update(filename='../escape'))]:
    p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='request_begin_upload'));mutate(p);f['cases'].append({'name':name,'valid':False,'payload':p})
for state in ['processing','quarantined','not_found','hash_mismatch']:
    p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='ack_get_command_status'))
    p['data']['result']={'state':state,'ackEventId':None}
    f['cases'].append({'name':'command_status_'+state,'valid':True,'payload':p})
reply=copy.deepcopy(next(c['payload']['data'] for c in f['cases'] if c['valid'] and c['payload'].get('kind')=='reply'))
reply['text']='a'*17000
p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='ack_get_messages'))
p['data']['result']['items']=[{'messageId':'assistant_long','requestId':reply['requestId'],'role':'assistant','reply':reply,'at':T}]
f['cases'].append({'name':'history_preserves_full_reply','valid':True,'payload':p})
p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='request_accept_vision_notice'));p['data']['accepted']=False
p.update(id='event_revoke_vision_notice',dedupeKey='command:client_revoke_vision_notice')
p['data']['clientMsgId']='client_revoke_vision_notice'
f['cases'].append({'name':'request_revoke_vision_notice','valid':True,'payload':p})
revoked=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='ack_accept_vision_notice'))
revoked.update(id='ack_revoke_vision_notice',dedupeKey='ack:client_revoke_vision_notice')
revoked['data'].update(refId=p['id'],clientMsgId=p['data']['clientMsgId'])
revoked['data']['result']['consentedAt']=None
revoked['data']['requestHash']=digest(p['data'])
f['cases'].append({'name':'ack_revoke_vision_notice','valid':True,'payload':revoked})
for name,source,fields,valid in [
    ('register_image_auto_apply','register_image',{'autoApply':True},True),
    ('register_image_rejects_non_boolean_auto','register_image',{'autoApply':'true'},False),
    ('adjust_position_selected_name','adjust_position',{'name':'浦发银行'},True),
    ('adjust_position_rejects_blank_name','adjust_position',{'name':' '},False),
]:
    p=copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='request_'+source))
    p['data'].update(fields)
    f['cases'].append({'name':name,'valid':valid,'payload':p})
pages=[c['payload']['data'] for c in f['cases'] if c['valid'] and c['payload'].get('kind')=='state_snapshot']
for page in pages:
    if page.get('manifest'):
        group=[p for p in pages if p['snapshotId']==page['snapshotId']]
        for key in page['manifest']:
            values=[p['meta'] for p in group if p.get('meta')] if key=='meta' else [v for p in group for v in p[key]]
            page['manifest'][key]={'count':len(values),'sha256':digest(values)}
archive = copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='request_recovery_reconcile'))
archive['data']['positions'] = []
archive['data']['decisions'] = [{'itemId':'non_trade_item','evidenceRecordIds':['non_trade_command','non_trade_ack'],
    'disposition':'archive_operation','note':'人工核对后保留恢复状态，旧非成交操作仅归档。'}]
f['cases'].append({'name':'request_archive_non_trade_recovery','valid':True,'payload':archive})
for name, edit in [('archive_without_review_note', lambda p:p['data']['decisions'][0].update(note=' ')),
                   ('archive_cannot_change_task', lambda p:p['data']['decisions'][0].update(taskResolution={'taskId':'old_task','status':'ignored'}))]:
    bad=copy.deepcopy(archive);edit(bad)
    f['cases'].append({'name':name,'valid':False,'payload':bad})
rule_only_risk = copy.deepcopy(next(c['payload'] for c in f['cases'] if c['name']=='task_update'))
rule_only_risk['data'].update(direction='risk',score=None,riskEventKey='suspension_episode',eventRevision=1)
rule_only_risk['data']['reasons'].update(verificationStatus='verified',riskEvidence=[
    {'type':'trading_suspension','ruleId':'test_rule','ruleVersion':'1','source':'test exchange',
     'sourceTime':T,'sourceRef':'test://suspension'},
])
f['cases'].append({'name':'task_verified_risk_without_score','valid':True,'payload':rule_only_risk})
unverified_risk = copy.deepcopy(rule_only_risk)
unverified_risk['data']['reasons']['riskEvidence'] = []
f['cases'].append({'name':'task_no_score_requires_risk_evidence','valid':False,'payload':unverified_risk})
scoreless_buy = copy.deepcopy(rule_only_risk)
scoreless_buy['data']['direction'] = 'buy'
f['cases'].append({'name':'task_buy_cannot_omit_score','valid':False,'payload':scoreless_buy})
save('fixtures.json',f)
# Transport schemas deliberately contain no arbitrary destination or cloud history cursor.
defs={}
defs['FileChunk']=obj({'transferId':ID,'fileId':ID,'index':{'type':'integer','minimum':0,'maximum':639},'totalChunks':{'type':'integer','minimum':1,'maximum':640},'chunkHash':HASH,'bytes':{'type':'string','minLength':4,'maxLength':43692,'pattern':'^[A-Za-z0-9+/]+={0,2}$'}})
defs['Peer']=obj({'online':BOOL,'incarnation':nullable(ID),'serverEpoch':nullable(ID)})
defs['hello']=obj({'v':enum(2),'type':enum('hello'),'incarnation':ID,'serverEpoch':nullable(ID)})
defs['welcome']=obj({'v':enum(2),'type':enum('welcome'),'pairId':ID,'relayBootId':ID,'connectionId':ID,'role':enum('app','local'),'heartbeatIntervalMs':enum(15000),'heartbeatTimeoutMs':enum(45000)})
defs['presence']=obj({'v':enum(2),'type':enum('presence'),'peer':ref('Peer')})
base={'v':enum(2),'type':enum('deliver'),'deliveryId':ID,'fromIncarnation':ID,'toIncarnation':ID,'payloadHash':HASH}
defs['deliver']={'oneOf':[obj({**base,'category':enum('business'),'payload':{'$ref':s['$id']}}),obj({**base,'category':enum('file_chunk'),'payload':ref('FileChunk')})]}
defs['receipt']=obj({'v':enum(2),'type':enum('receipt'),'deliveryId':ID,'payloadHash':HASH,'fromIncarnation':ID,'toIncarnation':ID})
defs['error']=obj({'v':enum(2),'type':enum('error'),'refDeliveryId':nullable(ID),'code':enum('BUSY','PEER_OFFLINE','INCARNATION_MISMATCH','INVALID_FRAME','HASH_MISMATCH','DELIVERY_ID_REUSED','DISK_FULL','UPLOAD_NOT_FOUND','CHUNK_CONFLICT'),'retryable':BOOL,'retryAfterMs':INT})
wire={'$schema':s['$schema'],'$id':'https://tickdesk.invalid/contracts/v2/relay.schema.json','title':'TickDesk relay v2','oneOf':[ref(n) for n in ['hello','welcome','presence','deliver','receipt','error']],'$defs':defs}
save('relay.schema.json',wire)
cases=[]
def case(name,payload,valid=True):cases.append({'name':name,'valid':valid,'payload':payload})
case('hello_app',{'v':2,'type':'hello','incarnation':'app_install_1','serverEpoch':None})
case('welcome',{'v':2,'type':'welcome','pairId':'pair_demo','relayBootId':'boot_1','connectionId':'conn_1','role':'app','heartbeatIntervalMs':15000,'heartbeatTimeoutMs':45000})
case('peer_offline',{'v':2,'type':'presence','peer':{'online':False,'incarnation':None,'serverEpoch':None}})
case('peer_online',{'v':2,'type':'presence','peer':{'online':True,'incarnation':'local_install_1','serverEpoch':'epoch_demo_a'}})
payload=next(c['payload'] for c in f['cases'] if c['name']=='request_act')
delivery={'v':2,'type':'deliver','deliveryId':'delivery_1','fromIncarnation':'app_install_1','toIncarnation':'local_install_1','category':'business','payloadHash':digest(payload),'payload':payload}
case('deliver_command',delivery)
case('receipt',{'v':2,'type':'receipt','deliveryId':'delivery_1','payloadHash':digest(payload),'fromIncarnation':'app_install_1','toIncarnation':'local_install_1'})
chunk={'transferId':'upload_demo','fileId':'file_demo','index':0,'totalChunks':1,'chunkHash':hashlib.sha256(b'abc').hexdigest(),'bytes':base64.b64encode(b'abc').decode()}
case('file_chunk',dict(delivery,deliveryId='delivery_2',category='file_chunk',payload=chunk,payloadHash=digest(chunk)))
case('busy',{'v':2,'type':'error','refDeliveryId':'delivery_1','code':'BUSY','retryable':True,'retryAfterMs':1000})
case('reject_route_spoof',dict(delivery,targetRole='local'),False)
case('reject_v1',dict(delivery,v=1),False)
case('reject_huge_block',dict(delivery,category='file_chunk',payload=dict(chunk,bytes='A'*43696)),False)
save('relay-fixtures.json',{'contractVersion':'2.0.0','cases':cases})
api={'openapi':'3.1.0','info':{'title':'TickDesk minimal relay (planned)','version':'2.0.0'},'servers':[{'url':'https://127.0.0.1:8443'}],'paths':{},'components':{'securitySchemes':{'RoleToken':{'type':'http','scheme':'bearer'}}}}
for path in ['/health/live','/health/ready']:
    api['paths'][path]={'get':{'operationId':path.rsplit('/',1)[1],'responses':{'200':{'description':'healthy'},'503':{'description':'not ready'}}}}
api['paths']['/relay/v2/connect']={'get':{'operationId':'connect','security':[{'RoleToken':[]}],'parameters':[{'name':'Sec-WebSocket-Protocol','in':'header','required':True,'schema':enum('tickdesk.v2')}],'responses':{str(c):{'description':desc} for c,desc in [(101,'Switching protocols; relay.schema.json defines WS messages'),(401,'Invalid role token'),(426,'Unsupported subprotocol'),(429,'Retry later')]}}}
save('cloud.openapi.json',api)
save('pairing.schema.json',dict(obj({'protocol':enum(2),'relayUrl':{'type':'string','pattern':'^wss://[^/]+:8443/relay/v2/connect$'},'pairId':ID,'appToken':{'type':'string','pattern':'^[A-Za-z0-9_-]{43}$'},'pins':dict(arr({'type':'string','pattern':'^sha256/[A-Za-z0-9+/]{43}=$'},2),minItems=1,uniqueItems=True)}),**{'$schema':s['$schema']}))
save('push.example.json',{**read('push.example.json'),'v':2})
save('hash-vectors.json',read('hash-vectors.json'))
print('Built v2: business commands',len(catalog['commands']),'business fixtures',len(f['cases']),'relay fixtures',len(cases))
