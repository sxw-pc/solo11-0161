import argparse
import hashlib
import json
from pathlib import Path
from .store import connect
from .source import read_file,ingest
from . import application,eligibility,allocation,shuttle,report,ride_route
HANDLERS={
 'application-import':{'household':application.household,'member':application.member,'document':application.document,'revoke_document':application.revoke_document},
 'eligibility-import':{'income':eligibility.income,'property':eligibility.property,'objection':eligibility.object,'resolve':eligibility.resolve},
 'allocation-import':{'unit':allocation.unit,'offer':allocation.offer,'sign':allocation.sign,'expire':allocation.expire,'deliver':allocation.deliver},
 'shuttle-import':{'shuttle_vehicle':shuttle.vehicle,'shuttle_request':shuttle.request},
 'ride-route-import':{'shuttle_vehicle':shuttle.vehicle,'place':ride_route.place,'stop':ride_route.stop,'segment':ride_route.segment,
                      'driver':ride_route.driver,'shift':ride_route.shift,'closure':ride_route.closure,
                      'wheelchair_auth':ride_route.wheelchair_auth,'ride_request':ride_route.request,
                      'ride_stop':ride_route.request_stop,'authorization':ride_route.authorize,
                      'hospital_rebook':ride_route.hospital_rebook,'consult':ride_route.consult,
                      'execute':ride_route.execute,'secure':ride_route.secure,'boarding':ride_route.board,
                      'delivery':ride_route.delivery,'observe_closure':ride_route.observe_closure},
}
def import_file(db,command,path):
    raw,events=read_file(path);results=[]
    with db:
        sha=hashlib.sha256(raw).hexdigest()
        if db.execute('SELECT id FROM source_file WHERE command=? AND sha=?',(command,sha)).fetchone():return [{'result':'file_replayed'}]
        for e in events:
            if e['kind'] not in HANDLERS[command]:raise ValueError('wrong subsystem event')
            if ingest(db,e):results.append({'id':e['id'],'result':HANDLERS[command][e['kind']](db,e)})
            else:results.append({'id':e['id'],'result':'event_replayed'})
        db.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(command,sha,raw))
    return results
def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('command',choices=[*HANDLERS,'demo','report','audit']);p.add_argument('--db',default='housing.sqlite');p.add_argument('--file')
    args=p.parse_args(argv);db=connect(args.db)
    if args.command=='demo':
        results=[]
        for cmd,name in [('application-import','application-ok.json'),('eligibility-import','eligibility-ok.json'),('allocation-import','allocation-ok.json')]:
            results.extend(import_file(db,cmd,Path(__file__).resolve().parents[1]/'fixtures'/name))
        result={'outcomes':results,'report':report.report(db)}
    elif args.command=='report':result=report.report(db)
    elif args.command=='audit':result=report.audit(db,HANDLERS)
    else:
        if not args.file:p.error('--file required')
        result={'outcomes':import_file(db,args.command,args.file),'report':report.report(db)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
