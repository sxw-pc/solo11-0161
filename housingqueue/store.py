import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS household(id TEXT PRIMARY KEY,community TEXT NOT NULL,applied_at TEXT NOT NULL,residence_from TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS member(id TEXT NOT NULL,household_id TEXT NOT NULL,relation TEXT NOT NULL,joined_at TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(id,household_id));
CREATE TABLE IF NOT EXISTS document(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,kind TEXT NOT NULL,subject TEXT NOT NULL,issued_at TEXT NOT NULL,received_at TEXT NOT NULL,expires_at TEXT NOT NULL,issuer TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS application_decision(id INTEGER PRIMARY KEY,household_id TEXT NOT NULL,state TEXT NOT NULL,accepted_at TEXT,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS income_check(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,month TEXT NOT NULL,cents INTEGER NOT NULL,issuer TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS property_check(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,area_m2 REAL NOT NULL,effective_at TEXT NOT NULL,issuer TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objection(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,opened_at TEXT NOT NULL,closes_at TEXT NOT NULL,reason TEXT NOT NULL,resolved_at TEXT,reviewer TEXT);
CREATE TABLE IF NOT EXISTS eligibility(id INTEGER PRIMARY KEY,household_id TEXT NOT NULL,state TEXT NOT NULL,priority INTEGER NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS queue_position(id INTEGER PRIMARY KEY,household_id TEXT NOT NULL,rank INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unit(id TEXT PRIMARY KEY,community TEXT NOT NULL,rooms INTEGER NOT NULL,accessible INTEGER NOT NULL,available_at TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,unit_id TEXT NOT NULL,offered_at TEXT NOT NULL,expires_at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,eligibility_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS signature(id TEXT PRIMARY KEY,offer_id TEXT NOT NULL,at TEXT NOT NULL,signer TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocation_dispute(id INTEGER PRIMARY KEY,offer_id TEXT NOT NULL,household_id TEXT NOT NULL,eligibility_version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS housing_delivery(id TEXT PRIMARY KEY,offer_id TEXT NOT NULL,household_id TEXT NOT NULL,unit_id TEXT NOT NULL,at TEXT NOT NULL,receiver TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shuttle_vehicle(id TEXT PRIMARY KEY,seats INTEGER NOT NULL,wheelchair_places INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shuttle_request(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,vehicle_id TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL,seats INTEGER NOT NULL,wheelchair INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shuttle_decision(request_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS shuttle_place(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS shuttle_segment(from_place TEXT NOT NULL,to_place TEXT NOT NULL,minutes INTEGER NOT NULL,PRIMARY KEY(from_place,to_place));
CREATE TABLE IF NOT EXISTS shuttle_driver(id TEXT PRIMARY KEY,max_drive_minutes INTEGER NOT NULL,max_continuous_minutes INTEGER NOT NULL,required_rest_minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shuttle_shift(id TEXT PRIMARY KEY,driver_id TEXT NOT NULL,vehicle_id TEXT NOT NULL,starts_at TEXT NOT NULL,ends_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shuttle_stop(id TEXT PRIMARY KEY,place_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shuttle_wheelchair_auth(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,granted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ride_request(id TEXT PRIMARY KEY,vehicle_id TEXT NOT NULL,shift_id TEXT NOT NULL,driver_id TEXT NOT NULL,origin_stop TEXT NOT NULL,starts_at TEXT NOT NULL,seats INTEGER NOT NULL,wheelchair_places INTEGER NOT NULL,context_only INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ride_request_stop(request_id TEXT NOT NULL,seq INTEGER NOT NULL,household_id TEXT NOT NULL,stop_id TEXT NOT NULL,place_id TEXT NOT NULL,kind TEXT NOT NULL,earliest TEXT,latest TEXT,wheelchair INTEGER NOT NULL,appointment_at TEXT,PRIMARY KEY(request_id,seq));
CREATE TABLE IF NOT EXISTS ride_authorization(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,household_id TEXT NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,kind TEXT NOT NULL,recorded_at TEXT NOT NULL,recorded_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS road_closure(id TEXT PRIMARY KEY,from_place TEXT NOT NULL,to_place TEXT NOT NULL,closed_at TEXT NOT NULL,until TEXT);
CREATE TABLE IF NOT EXISTS hospital_rebook(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,request_id TEXT NOT NULL,at TEXT NOT NULL,new_appointment_at TEXT NOT NULL,certificate TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ride_secure(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,household_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,witness_a TEXT NOT NULL,witness_b TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ride_booking(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,vehicle_id TEXT NOT NULL,shift_id TEXT NOT NULL,driver_id TEXT NOT NULL,origin_place TEXT NOT NULL,depart_at TEXT NOT NULL,arrive_at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ride_leg(id INTEGER PRIMARY KEY,booking_id TEXT NOT NULL,seq INTEGER NOT NULL,stop_seq INTEGER NOT NULL,from_place TEXT NOT NULL,to_place TEXT NOT NULL,depart_at TEXT NOT NULL,arrive_at TEXT NOT NULL,drive_minutes INTEGER NOT NULL,break_minutes INTEGER NOT NULL,passengers_in_vehicle INTEGER NOT NULL,wheelchair_in_vehicle INTEGER NOT NULL,status TEXT NOT NULL,run_at TEXT,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ride_boarding(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,household_id TEXT NOT NULL,stop_seq INTEGER NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,witness_a TEXT NOT NULL,witness_b TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ride_stop_state(request_id TEXT NOT NULL,seq INTEGER NOT NULL,state TEXT NOT NULL,at TEXT,reason TEXT NOT NULL,PRIMARY KEY(request_id,seq));
CREATE TABLE IF NOT EXISTS ride_household_report(request_id TEXT NOT NULL,household_id TEXT NOT NULL,original_window_start TEXT,original_window_end TEXT,authorized_window_start TEXT,authorized_window_end TEXT,boarded_at TEXT,alighted_at TEXT,appointment_at TEXT,caught_original INTEGER,verdict TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(request_id,household_id));
CREATE TABLE IF NOT EXISTS ride_consultation(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,household_id TEXT NOT NULL,at TEXT NOT NULL,answer TEXT NOT NULL);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))
