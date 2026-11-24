app_name = "todo_privacy"
app_title = "ToDo Privacy"
app_publisher = 'Nexxttech Solutions Ja'
app_description = "Keeps ERPNext ToDos private to the people who created, assigned or hold them"
app_email = 'nexxttechja@gmail.com'
app_license = "MIT"

# ----------------------------------------------------------
# Enforce the exact ToDo permission baseline on install and on every migrate,
# and check for drift daily. See todo_privacy/policy.py.
# ----------------------------------------------------------
after_install = "todo_privacy.install.after_install"
after_migrate = "todo_privacy.install.after_migrate"

scheduler_events = {
	"daily": ["todo_privacy.install.daily_check"],
}

# ----------------------------------------------------------
# Permission decisions from the stored record, never the submitted one.
# Comment and Version copy ToDo text and are readable by System Manager,
# so ToDo-related rows there follow the same participant rule.
# ----------------------------------------------------------
has_permission = {
	"ToDo": "todo_privacy.permissions.todo_has_permission",
	"Comment": "todo_privacy.permissions.comment_has_permission",
	"Version": "todo_privacy.permissions.version_has_permission",
	"Communication": "todo_privacy.permissions.communication_has_permission",
}

permission_query_conditions = {
	"Comment": "todo_privacy.permissions.comment_query_conditions",
	"Version": "todo_privacy.permissions.version_query_conditions",
	"Communication": "todo_privacy.permissions.communication_query_conditions",
}

doc_events = {
	"ToDo": {
		"before_insert": "todo_privacy.guards.todo_before_insert",
		"validate": "todo_privacy.guards.todo_validate",
	},
	"Comment": {
		"before_insert": "todo_privacy.guards.strip_assigned_comment",
		"validate": [
			"todo_privacy.guards.strip_assigned_comment",
			"todo_privacy.guards.block_linking_shared_record",
		],
	},
	"Version": {"validate": "todo_privacy.guards.block_linking_shared_record"},
	"Communication": {"validate": "todo_privacy.guards.block_linking_shared_record"},
	"File": {"validate": "todo_privacy.guards.block_linking_shared_record"},
	"DocShare": {
		"before_insert": "todo_privacy.guards.block_todo_share",
		"validate": "todo_privacy.guards.block_todo_share",
	},
}

# ERPNext's Email Digest lists ToDos by owner/assigned_by and misses ToDos
# assigned to the recipient. v14 uses the LAST app's class override.
override_doctype_class = {
	"Email Digest": "todo_privacy.email_digest.EmailDigest",
}

# ----------------------------------------------------------
# Readers that bypass ToDo permissions, and the Role Permission Manager
# endpoints (so a change to ToDo permissions in the UI is put back at once).
# v14 uses the FIRST app's override; policy.status() checks they resolve here.
# ----------------------------------------------------------
override_whitelisted_methods = {
	"frappe.desk.form.load.getdoc": "todo_privacy.overrides.getdoc",
	"frappe.desk.form.load.get_docinfo": "todo_privacy.overrides.get_docinfo",
	"frappe.desk.form.load.get_communications": "todo_privacy.overrides.get_communications",
	"frappe.desk.listview.get_group_by_count": "todo_privacy.overrides.get_group_by_count",
	"erpnext.crm.utils.get_open_activities": "todo_privacy.overrides.get_open_activities",
	"frappe.core.page.permission_manager.permission_manager.add": "todo_privacy.overrides.permission_manager_add",
	"frappe.core.page.permission_manager.permission_manager.update": "todo_privacy.overrides.permission_manager_update",
	"frappe.core.page.permission_manager.permission_manager.remove": "todo_privacy.overrides.permission_manager_remove",
	"frappe.core.page.permission_manager.permission_manager.reset": "todo_privacy.overrides.permission_manager_reset",
}
