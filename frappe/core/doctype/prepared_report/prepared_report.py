# Copyright (c) 2018, Frappe Technologies and contributors
# License: MIT. See LICENSE


import json

from rq import get_current_job

import frappe
from frappe.desk.form.load import get_attachments
from frappe.desk.query_report import generate_report_result
from frappe.model.document import Document
from frappe.monitor import add_data_to_monitor
from frappe.utils import gzip_compress, gzip_decompress
from frappe.utils.background_jobs import enqueue


class PreparedReport(Document):
	def before_insert(self):
		self.status = "Queued"
		self.report_start_time = frappe.utils.now()

	def after_insert(self):
		enqueue(
			generate_report,
			queue="long",
			prepared_report=self.name,
			timeout=1800,
			enqueue_after_commit=True,
		)

	def update_job_id(self, job_id):
		frappe.db.set_value(self.doctype, self.name, "job_id", job_id, update_modified=False)
		frappe.db.commit()


def generate_report(prepared_report):
	instance = frappe.get_doc("Prepared Report", prepared_report)
	report = frappe.get_doc("Report", instance.report_name)
	instance.update_job_id(get_current_job().id)

	add_data_to_monitor(report=instance.report_name)

	try:
		report.custom_columns = []

		if report.report_type == "Custom Report":
			custom_report_doc = report
			reference_report = custom_report_doc.reference_report
			report = frappe.get_doc("Report", reference_report)
			if custom_report_doc.json:
				data = json.loads(custom_report_doc.json)
				if data:
					report.custom_columns = data["columns"]

		result = generate_report_result(report=report, filters=instance.filters, user=instance.owner)
		create_json_gz_file(result["result"], "Prepared Report", instance.name)

		instance.status = "Completed"
		instance.report_end_time = frappe.utils.now()
		instance.save(ignore_permissions=True)

	except Exception:
		instance.status = "Error"
		instance.error_message = frappe.get_traceback()
		instance.save(ignore_permissions=True)

	frappe.publish_realtime(
		"report_generated",
		{"report_name": instance.report_name, "name": instance.name},
		user=frappe.session.user,
	)


@frappe.whitelist()
def get_reports_in_queued_state(report_name, filters):
	reports = frappe.get_all(
		"Prepared Report",
		filters={
			"report_name": report_name,
			"filters": process_filters_for_prepared_report(filters),
			"status": "Queued",
		},
	)
	return reports


def delete_expired_prepared_reports():
	system_settings = frappe.get_single("System Settings")
	enable_auto_deletion = system_settings.enable_prepared_report_auto_deletion
	if enable_auto_deletion:
		expiry_period = system_settings.prepared_report_expiry_period
		prepared_reports_to_delete = frappe.get_all(
			"Prepared Report",
			filters={"creation": ["<", frappe.utils.add_days(frappe.utils.now(), -expiry_period)]},
		)

		batches = frappe.utils.create_batch(prepared_reports_to_delete, 100)
		for batch in batches:
			args = {
				"reports": batch,
			}
			enqueue(method=delete_prepared_reports, job_name="delete_prepared_reports", **args)


@frappe.whitelist()
def delete_prepared_reports(reports):
	reports = frappe.parse_json(reports)
	for report in reports:
		frappe.delete_doc(
			"Prepared Report", report["name"], ignore_permissions=True, delete_permanently=True
		)


def process_filters_for_prepared_report(filters):
	if isinstance(filters, str):
		filters = json.loads(filters)

	# This looks like an insanity but, without this it'd be very hard to find Prepared Reports matching given condition
	# We're ensuring that spacing is consistent. e.g. JS seems to put no spaces after ":", Python on the other hand does.
	# We are also ensuring that order of keys is same so generated JSON string will be identical too.
	# PS: frappe.as_json sorts keys
	return frappe.as_json(filters, indent=None, separators=(",", ":"))


def create_json_gz_file(data, dt, dn):
	# Storing data in CSV file causes information loss
	# Reports like P&L Statement were completely unsuable because of this
	json_filename = "{}.json.gz".format(
		frappe.utils.data.format_datetime(frappe.utils.now(), "Y-m-d-H:M")
	)
	encoded_content = frappe.safe_encode(frappe.as_json(data))
	compressed_content = gzip_compress(encoded_content)

	# Call save() file function to upload and attach the file
	_file = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": json_filename,
			"attached_to_doctype": dt,
			"attached_to_name": dn,
			"content": compressed_content,
			"is_private": 1,
		}
	)
	_file.save(ignore_permissions=True)


@frappe.whitelist()
def download_attachment(dn):
	attachment = get_attachments("Prepared Report", dn)[0]
	frappe.local.response.filename = attachment.file_name[:-2]
	attached_file = frappe.get_doc("File", attachment.name)
	frappe.local.response.filecontent = gzip_decompress(attached_file.get_content())
	frappe.local.response.type = "binary"


def get_permission_query_condition(user):
	if not user:
		user = frappe.session.user
	if user == "Administrator":
		return None

	from frappe.utils.user import UserPermissions

	user = UserPermissions(user)

	if "System Manager" in user.roles:
		return None

	reports = [frappe.db.escape(report) for report in user.get_all_reports().keys()]

	return """`tabPrepared Report`.report_name in ({reports})""".format(reports=",".join(reports))


def has_permission(doc, user):
	if not user:
		user = frappe.session.user
	if user == "Administrator":
		return True

	from frappe.utils.user import UserPermissions

	user = UserPermissions(user)

	if "System Manager" in user.roles:
		return True

	return doc.report_name in user.get_all_reports().keys()
