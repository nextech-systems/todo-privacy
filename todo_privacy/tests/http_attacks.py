"""HTTP-level attack checks against a RUNNING staging web server.

Not part of run-tests: it commits synthetic users and records, then deletes
them. Staging only.

    cd frappe-bench/sites
    ../env/bin/python ../apps/todo_privacy/todo_privacy/tests/http_attacks.py \
        --site <staging-site> --url http://127.0.0.1:8000

Prints one line per check and a JSON summary; exits non-zero on any failure.
"""

import argparse
import json
import secrets
import sys

import requests

import frappe

DOMAIN = "tp-http.example.invalid"
MARKER = "zz-http-marker-5519"


def setup(site):
	frappe.init(site=site, sites_path=".")
	frappe.connect()
	frappe.set_user("Administrator")
	frappe.flags.mute_emails = True
	password = secrets.token_urlsafe(16)
	users = {}
	for key, roles in (("outsider", ["System Manager"]), ("assignee", []), ("assigner", ["System Manager"]), ("stranger", [])):
		email = f"{key}@{DOMAIN}"
		if not frappe.db.exists("User", email):
			user = frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": key, "send_welcome_email": 0, "user_type": "System User"}
			)
			user.flags.no_welcome_mail = True
			user.insert(ignore_permissions=True)
			if roles:
				user.add_roles(*roles)
		from frappe.utils.password import update_password

		update_password(email, password)
		users[key] = email
	note = frappe.get_doc({"doctype": "Note", "title": "tp http note", "public": 1}).insert()
	todo = frappe.get_doc(
		{
			"doctype": "ToDo",
			"description": MARKER,
			"allocated_to": users["assignee"],
			"assigned_by": users["assigner"],
			"reference_type": "Note",
			"reference_name": note.name,
		}
	).insert(ignore_permissions=True)
	comment = frappe.get_doc(
		{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "ToDo", "reference_name": todo.name, "content": MARKER}
	).insert(ignore_permissions=True)
	users["_todo_comment"] = comment.name
	# one Communication filed on the ToDo, one filed on the public Note with a ToDo timeline link
	comms = []
	for ref, links in ((("ToDo", todo.name), []), (("Note", note.name), [todo.name])):
		c = frappe.get_doc(
			{"doctype": "Communication", "communication_type": "Communication", "communication_medium": "Other",
			 "subject": "tp http", "content": MARKER, "reference_doctype": ref[0], "reference_name": ref[1]}
		)
		for link in links:
			c.append("timeline_links", {"link_doctype": "ToDo", "link_name": link})
		comms.append(c.insert(ignore_permissions=True).name)
	users["_comms"] = comms
	frappe.get_doc(
		{"doctype": "Comment", "comment_type": "Assigned", "reference_doctype": "Note", "reference_name": note.name, "content": f"A assigned B: {MARKER}"}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return users, password, todo.name, note.name


def cleanup(users, note):
	frappe.set_user("Administrator")
	for t in frappe.get_all("ToDo", filters={"reference_name": note}, pluck="name"):
		frappe.db.delete("Comment", {"reference_doctype": "ToDo", "reference_name": t})
		frappe.db.delete("Version", {"ref_doctype": "ToDo", "docname": t})
		frappe.db.delete("ToDo", t)
	frappe.db.delete("Comment", {"reference_doctype": "Note", "reference_name": note})
	for c in users.get("_comms", []):
		frappe.db.delete("Communication Link", {"parent": c})
		frappe.db.delete("Communication", c)
	frappe.db.delete("DocShare", {"user": ("in", [u for k, u in users.items() if not k.startswith("_")])})
	frappe.db.delete("Note", note)
	for key, email in users.items():
		if not key.startswith("_"):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	frappe.db.commit()


def session(url, host, user, password):
	s = requests.Session()
	s.headers["Host"] = host
	r = s.post(f"{url}/api/method/login", data={"usr": user, "pwd": password}, timeout=30)
	r.raise_for_status()
	return s


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--url", default="http://127.0.0.1:8000")
	args = parser.parse_args()

	users, password, todo, note = setup(args.site)
	results = []

	def check(label, ok, detail=""):
		results.append({"check": label, "pass": bool(ok), "detail": detail})
		print(("PASS " if ok else "FAIL ") + label + (f" ({detail})" if detail else ""))

	try:
		out = session(args.url, args.site, users["outsider"], password)
		original = frappe.get_doc("ToDo", todo).as_dict()
		forged = json.loads(frappe.as_json(original))
		forged.update({"assigned_by": users["outsider"], "description": "tampered"})

		r = out.get(f"{args.url}/api/resource/ToDo", params={"fields": '["name"]', "limit_page_length": 0}, timeout=30)
		check("list ToDo excludes others' ToDos", r.ok and todo not in [d["name"] for d in r.json()["data"]], r.status_code)

		r = out.get(f"{args.url}/api/resource/ToDo/{todo}", timeout=30)
		check("GET someone else's ToDo is refused", r.status_code == 403, r.status_code)

		r = out.post(f"{args.url}/api/method/frappe.client.save", data={"doc": json.dumps(forged)}, timeout=30)
		check("forged assigned_by via frappe.client.save is refused", r.status_code == 403, r.status_code)

		r = out.post(
			f"{args.url}/api/method/frappe.desk.form.save.savedocs",
			data={"doc": json.dumps(forged), "action": "Save"},
			timeout=30,
		)
		check("forged assigned_by via savedocs is refused", r.status_code == 403, r.status_code)

		r = out.put(f"{args.url}/api/resource/ToDo/{todo}", json={"assigned_by": users["outsider"]}, timeout=30)
		check("PUT /api/resource/ToDo is refused", r.status_code == 403, r.status_code)

		r = out.delete(f"{args.url}/api/resource/ToDo/{todo}", timeout=30)
		check("DELETE someone else's ToDo is refused", r.status_code == 403, r.status_code)

		r = out.post(
			f"{args.url}/api/method/frappe.share.add",
			data={"doctype": "ToDo", "name": todo, "user": users["outsider"]},
			timeout=30,
		)
		check("sharing a ToDo is refused", r.status_code == 403, r.status_code)

		frappe.db.rollback()
		stored = frappe.db.get_value("ToDo", todo, ["assigned_by", "description"], as_dict=True)
		check(
			"ToDo unchanged after attacks",
			bool(stored) and stored.assigned_by == users["assigner"] and stored.description == MARKER,
			"deleted" if not stored else "",
		)

		r = out.get(
			f"{args.url}/api/resource/Comment",
			params={"filters": json.dumps({"reference_doctype": "ToDo"}), "fields": '["content"]', "limit_page_length": 0},
			timeout=30,
		)
		check("System Manager cannot list ToDo comments", r.ok and MARKER not in r.text, r.status_code)

		r = out.get(
			f"{args.url}/api/resource/Version",
			params={"filters": json.dumps({"ref_doctype": "ToDo", "docname": todo}), "limit_page_length": 0},
			timeout=30,
		)
		check("System Manager cannot list ToDo versions", r.ok and not r.json()["data"], r.status_code)

		r = out.get(f"{args.url}/api/method/frappe.desk.form.load.getdoc", params={"doctype": "Note", "name": note}, timeout=30)
		check("form sidebar/timeline of the Note hides ToDo text", r.ok and MARKER not in r.text, r.status_code)

		r = out.get(
			f"{args.url}/api/method/frappe.desk.listview.get_group_by_count",
			params={"doctype": "Note", "current_filters": json.dumps({"name": note}), "field": "assigned_to"},
			timeout=30,
		)
		check("assigned-to sidebar counts still work", r.ok, r.status_code)

		# A participant who is a System Manager tries to share the ToDo's comment
		comment = users["_todo_comment"]
		sharer = session(args.url, args.site, users["assigner"], password)
		r = sharer.post(
			f"{args.url}/api/method/frappe.share.add",
			data={"doctype": "Comment", "name": comment, "user": users["stranger"], "read": 1},
			timeout=30,
		)
		check("participant cannot share the ToDo's comment", r.status_code == 403, r.status_code)
		r = sharer.post(
			f"{args.url}/api/method/frappe.share.add",
			data={"doctype": "Comment", "name": comment, "everyone": 1, "read": 1},
			timeout=30,
		)
		check("participant cannot share the ToDo's comment with everyone", r.status_code == 403, r.status_code)

		stranger = session(args.url, args.site, users["stranger"], password)
		r = stranger.get(f"{args.url}/api/resource/Comment/{comment}", timeout=30)
		check("recipient cannot open the ToDo's comment", r.status_code == 403 and MARKER not in r.text, r.status_code)
		r = stranger.get(f"{args.url}/api/resource/Comment", params={"fields": '["content"]', "limit_page_length": 0}, timeout=30)
		check("recipient cannot list the ToDo's comment", MARKER not in r.text, r.status_code)

		# Communications: list leak (finding 1) and secondary timeline link (finding 2)
		primary_comm, secondary_comm = users["_comms"]
		r = out.get(
			f"{args.url}/api/resource/Communication",
			params={"fields": '["name","content"]', "filters": json.dumps([["name", "in", users["_comms"]]]), "limit_page_length": 0},
			timeout=30,
		)
		check("unrelated System Manager cannot list ToDo Communications", r.ok and MARKER not in r.text, r.status_code)
		r = out.get(f"{args.url}/api/resource/Communication/{secondary_comm}", timeout=30)
		check("unrelated System Manager cannot open a secondary-linked Communication", r.status_code == 403, r.status_code)
		r = sharer.post(
			f"{args.url}/api/method/frappe.share.add",
			data={"doctype": "Communication", "name": secondary_comm, "user": users["stranger"], "read": 1},
			timeout=30,
		)
		check("participant cannot share a secondary-linked Communication", r.status_code == 403, r.status_code)
		r = stranger.get(f"{args.url}/api/resource/Communication/{secondary_comm}", timeout=30)
		check("recipient cannot open the secondary-linked Communication", MARKER not in r.text, r.status_code)
		r = out.get(
			f"{args.url}/api/method/frappe.desk.form.load.get_communications",
			params={"doctype": "Note", "name": note},
			timeout=30,
		)
		check("Note timeline hides the ToDo-linked Communication", r.ok and MARKER not in r.text, r.status_code)
		r = sharer.get(f"{args.url}/api/resource/Communication/{secondary_comm}", timeout=30)
		check("participant System Manager can open it (positive control)", r.ok and MARKER in r.text, r.status_code)

		mine = session(args.url, args.site, users["assignee"], password)
		r = mine.get(f"{args.url}/api/resource/ToDo/{todo}", timeout=30)
		check("assignee can open their ToDo", r.ok and MARKER in r.text, r.status_code)
	finally:
		frappe.db.rollback()
		cleanup(users, note)

	failed = [r for r in results if not r["pass"]]
	print(json.dumps({"checks": len(results), "failed": len(failed)}))
	frappe.destroy()
	sys.exit(1 if failed else 0)


if __name__ == "__main__":
	main()
