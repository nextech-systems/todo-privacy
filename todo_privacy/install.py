"""Install, migrate, scheduled and Role Permission Manager entry points.

Order matters: snapshot first, then change, so every change can be undone
exactly with ``todo_privacy.snapshot``.
"""

import frappe

from todo_privacy import policy, snapshot


def apply_baseline(reason):
	"""Snapshot if anything has drifted, then enforce the baseline. Returns what was corrected."""
	corrected = []
	if policy.check():
		snapshot.take_permissions(reason=f"before {reason}")
		corrected = policy.enforce_permissions(reason=reason)
	return corrected


def _lock_name():
	# MariaDB advisory locks are server-wide; include the site database so
	# sites sharing a database server do not block each other.
	return f"todo_privacy_apply:{frappe.conf.db_name}"


class AlreadyRunning(Exception):
	pass


def _lock():
	"""Database advisory lock so two runs (migrate, daily job, Role Permission
	Manager) never snapshot and rewrite at the same time. Released when the
	connection ends, so a crashed run cannot leave it stuck."""
	if not frappe.db.sql("select get_lock(%s, 0)", (_lock_name(),))[0][0]:
		raise AlreadyRunning(_lock_name())


def _unlock():
	frappe.db.sql("select release_lock(%s)", (_lock_name(),))


def _apply(reason, commit=True):
	try:
		_lock()
	except AlreadyRunning:
		policy._log("ToDo privacy run skipped", {"reason": reason, "why": "another run holds the lock"})
		return None
	try:
		corrected = apply_baseline(reason)
		policy.backfill_assigned_by(reason=reason)
		policy.sanitise_assignment_comments(reason=reason)
		if commit:
			frappe.db.commit()
		return corrected
	finally:
		_unlock()


def after_install():
	_apply("install")


def after_migrate():
	_apply("migrate")


def daily_check():
	"""Re-apply anything that has drifted; corrections are written to the Error Log."""
	if policy.check() or policy.backfill_candidates_count() or policy.assignment_comment_candidates_count():
		_apply("daily drift check")
