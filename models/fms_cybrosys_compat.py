"""
fms_cybrosys_compat.py — Patches for known Cybrosys / core incompatibilities.

financial.report (base_accounting_kit) is a TransientModel that inherits
account.report. Odoo's autovacuum calls unlink() on old transient rows, but
account.report.unlink() raises UserError for reports that have variants.
Result: a noisy ERROR in the log every autovacuum cycle.

Fix: catch UserError in the vacuum method so the cron job keeps running.
"""

from odoo import models
from odoo.exceptions import UserError


class FinancialReportVacuumFix(models.TransientModel):
    _inherit = 'financial.report'

    def _transient_clean_rows_older_than(self, seconds):
        try:
            return super()._transient_clean_rows_older_than(seconds)
        except UserError:
            pass
