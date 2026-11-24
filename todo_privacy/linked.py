"""Records that carry ToDo content, and the shares that could expose them.

Frappe lets a share override a permission denial: shared names are OR'd into
list queries, and a shared document is readable even when every permission
hook says no (frappe/permissions.py, false_if_not_shared). So a share on a
ToDo, or on any record that holds ToDo content, would bypass the participant
rule. This module defines that scope once; the share guard, the drift check,
the cleanup and the snapshots all use it.

Linked record types and the fields that tie them to a ToDo:
- Comment:       reference_doctype / reference_name (comments on the ToDo)
- Version:       ref_doctype / docname (the ToDo's change history)
- Communication: reference_doctype / reference_name (ToDo is an email target),
                 plus secondary timeline links (child table Communication Link)
- File:          attached_to_doctype / attached_to_name (ToDo attachments)
"""

import frappe

TODO = "ToDo"
LINKED = {
	"Comment": ("reference_doctype", "reference_name"),
	"Version": ("ref_doctype", "docname"),
	"Communication": ("reference_doctype", "reference_name"),
	"File": ("attached_to_doctype", "attached_to_name"),
}


def stored_todo_refs(doctype, name):
	"""ToDo names a stored record points at: primary link, plus Communication timeline links."""
	fields = LINKED.get(doctype)
	if not fields or not name:
		return []
	refs = []
	row = frappe.db.get_value(doctype, name, list(fields), as_dict=True)
	if row and row.get(fields[0]) == TODO and row.get(fields[1]):
		refs.append(row.get(fields[1]))
	if doctype == "Communication":
		refs += frappe.get_all(
			"Communication Link",
			filters={"parenttype": "Communication", "parent": name, "link_doctype": TODO},
			pluck="link_name",
		)
	return refs


def submitted_todo_refs(doc):
	"""ToDo names an in-memory (possibly unsaved) record points at, including pending timeline links."""
	fields = LINKED.get(doc.doctype)
	if not fields:
		return []
	refs = []
	if doc.get(fields[0]) == TODO and doc.get(fields[1]):
		refs.append(doc.get(fields[1]))
	if doc.doctype == "Communication":
		refs += [row.link_name for row in doc.get("timeline_links") or [] if row.link_doctype == TODO and row.link_name]
	return refs


def is_todo_content(doctype, name):
	"""True if ``doctype``/``name`` is a ToDo or a stored record linked to one (primary or secondary)."""
	if doctype == TODO:
		return True
	return bool(stored_todo_refs(doctype, name))


# Share scopes recorded in permission snapshots:
#   "direct"             before 1.2.2: shares on ToDo records only
#   "todo-and-linked"    1.2.2: plus records linked to a ToDo by their primary field
#   "todo-and-linked-v2" 1.2.3+: plus Communications linked by a timeline link
SCOPE_DIRECT = "direct"
SCOPE_V1 = "todo-and-linked"
SCOPE_V2 = "todo-and-linked-v2"
CURRENT_SCOPE = SCOPE_V2
KNOWN_SCOPES = (SCOPE_DIRECT, SCOPE_V1, SCOPE_V2)


def _scope_sql(scope=CURRENT_SCOPE):
	if scope not in KNOWN_SCOPES:
		frappe.throw(f"Unknown share scope {scope!r}")
	parts = ["`share_doctype` = 'ToDo'"]
	if scope == SCOPE_DIRECT:
		return parts[0]
	for doctype, (dt_field, _name_field) in LINKED.items():
		parts.append(
			f"(`share_doctype` = '{doctype}' and `share_name` in"
			f" (select `name` from `tab{doctype}` where `{dt_field}` = 'ToDo'))"
		)
	if scope == SCOPE_V2:
		parts.append(
			"(`share_doctype` = 'Communication' and `share_name` in"
			" (select `parent` from `tabCommunication Link`"
			" where `parenttype` = 'Communication' and `link_doctype` = 'ToDo'))"
		)
	return " or ".join(parts)


def scoped_shares(scope=CURRENT_SCOPE):
	"""Full rows of every DocShare in ``scope``, in a stable order."""
	return frappe.db.sql(
		f"select * from `tabDocShare` where {_scope_sql(scope)} order by creation asc, name asc",
		as_dict=True,
	)


def scoped_share_count():
	return frappe.db.sql(f"select count(*) from `tabDocShare` where {_scope_sql()}")[0][0]


def delete_scoped_shares(scope=CURRENT_SCOPE):
	names = [r.name for r in frappe.db.sql(f"select name from `tabDocShare` where {_scope_sql(scope)}", as_dict=True)]
	if names:
		frappe.db.delete("DocShare", {"name": ("in", names)})
	return len(names)
