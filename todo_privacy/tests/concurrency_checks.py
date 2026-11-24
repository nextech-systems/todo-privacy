"""Concurrency checks in separate processes (separate DB connections). Staging only.

Commits synthetic users and ToDos, then deletes them.

    cd frappe-bench/sites
    ../env/bin/python ../apps/todo_privacy/todo_privacy/tests/concurrency_checks.py --site <staging-site>

1. N users insert ToDos at the same moment, each forging assigned_by as
   someone else: every ToDo must record its real creator.
2. While one process holds the run lock, migrate/daily/permission-manager
   runs in other processes must skip, not run in parallel.
3. Two participants save the same ToDo from stale copies at once: exactly
   one save wins; the other gets Frappe's timestamp conflict.
"""

import argparse
import json
import multiprocessing as mp
import sys
import time

import frappe

DOMAIN = "tp-conc.example.invalid"
BOSS = f"boss@{DOMAIN}"


def _connect(site):
	frappe.init(site=site, sites_path=".")
	frappe.connect()
	frappe.flags.mute_emails = True


def _ensure_user(email):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0})
		u.flags.no_welcome_mail = True
		u.insert(ignore_permissions=True)


def worker_insert(args):
	site, user, barrier_at = args
	_connect(site)
	try:
		frappe.set_user(user)
		while time.time() < barrier_at:
			time.sleep(0.005)
		doc = frappe.get_doc(
			{"doctype": "ToDo", "description": "conc", "allocated_to": user, "assigned_by": BOSS}
		).insert()
		frappe.db.commit()
		return user, doc.assigned_by
	finally:
		frappe.destroy()


def worker_apply(args):
	site, barrier_at = args
	_connect(site)
	try:
		from todo_privacy import install

		frappe.set_user("Administrator")
		while time.time() < barrier_at:
			time.sleep(0.005)
		return "skipped" if install._apply("concurrency check") is None else "ran"
	finally:
		frappe.destroy()


def worker_hold_lock(args):
	site, seconds = args
	_connect(site)
	try:
		from todo_privacy import install

		install._lock()
		time.sleep(seconds)
		install._unlock()
		return "held"
	finally:
		frappe.destroy()


def worker_save(args):
	site, user, todo, new_priority, barrier_at = args
	_connect(site)
	try:
		frappe.set_user(user)
		doc = frappe.get_doc("ToDo", todo)
		while time.time() < barrier_at:
			time.sleep(0.005)
		doc.priority = new_priority
		try:
			doc.save()
			frappe.db.commit()
			return "saved"
		except frappe.TimestampMismatchError:
			frappe.db.rollback()
			return "conflict"
	finally:
		frappe.destroy()


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--workers", type=int, default=20)
	args = parser.parse_args()
	site = args.site

	_connect(site)
	frappe.set_user("Administrator")
	users = [f"w{i}@{DOMAIN}" for i in range(args.workers)]
	for email in [BOSS, *users]:
		_ensure_user(email)
	frappe.db.commit()
	frappe.destroy()

	results = {}
	ctx = mp.get_context("spawn")
	try:
		with ctx.Pool(args.workers) as pool:
			at = time.time() + 3
			rows = pool.map(worker_insert, [(site, u, at) for u in users])
		wrong = [u for u, by in rows if by != u]
		results["1 parallel inserts record the real creator"] = {"pass": not wrong, "inserted": len(rows), "wrong": len(wrong)}

		with ctx.Pool(5) as pool:
			holder = pool.apply_async(worker_hold_lock, ((site, 6),))
			time.sleep(2)
			at = time.time() + 1
			outcomes = pool.map(worker_apply, [(site, at)] * 4)
			holder.get()
		results["2 runs skip while the lock is held"] = {"pass": outcomes.count("skipped") == 4, "outcomes": outcomes}

		_connect(site)
		frappe.set_user("Administrator")
		shared = frappe.get_doc(
			{"doctype": "ToDo", "description": "conc shared", "allocated_to": users[0], "assigned_by": users[1]}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.destroy()
		with ctx.Pool(2) as pool:
			at = time.time() + 3
			saves = pool.map(
				worker_save, [(site, users[0], shared.name, "High", at), (site, users[1], shared.name, "Low", at)]
			)
		results["3 stale parallel saves: one wins"] = {"pass": sorted(saves) == ["conflict", "saved"], "outcomes": saves}
	finally:
		_connect(site)
		frappe.set_user("Administrator")
		# only ToDos belonging to this run's synthetic users
		synthetic = frappe.get_all("ToDo", filters={"allocated_to": ("in", [BOSS, *users])}, pluck="name")
		synthetic += frappe.get_all("ToDo", filters={"assigned_by": ("in", [BOSS, *users])}, pluck="name")
		for name in sorted(set(synthetic)):
			frappe.db.delete("Comment", {"reference_doctype": "ToDo", "reference_name": name})
			frappe.db.delete("Version", {"ref_doctype": "ToDo", "docname": name})
			frappe.db.delete("ToDo", name)
		for email in [BOSS, *users]:
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.destroy()

	for label, r in results.items():
		print(("PASS " if r["pass"] else "FAIL ") + label + " " + json.dumps({k: v for k, v in r.items() if k != "pass"}))
	failed = [r for r in results.values() if not r["pass"]]
	print(json.dumps({"checks": len(results), "failed": len(failed)}))
	sys.exit(1 if failed or len(results) < 3 else 0)


if __name__ == "__main__":
	main()
