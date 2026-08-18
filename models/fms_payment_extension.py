"""
fms_payment_extension.py — Extend account.payment with FMS shift context.

Every cash movement at a forecourt station must be traceable to a shift and
(where applicable) to the responsible attendant. This module adds that context
to Odoo's native payment model without duplicating its accounting logic.

FMS payment contexts (fms_payment_context field):
  customer_receipt  — Customer pays outstanding invoice at pump (cash inflow)
  vendor_payment    — Authorised vendor payment from shift cash (cash outflow)
  cash_float        — Float issued to attendant at shift start (cash inflow, not revenue)
  cash_drop         — Mid-shift cash drop to safe (reduces attendant's expected holding)
  cash_pickup       — Supervisor picks up cash from attendant (same as drop but named)
  expense           — Small expense paid directly from shift cash (cash outflow)
  other             — Any other shift-linked cash movement requiring audit trail

Impact on attendant cash reconciliation:
  customer_receipt  → adds to total_in  (attendant received cash)
  cash_float        → adds to total_in  (attendant received float — not revenue)
  cash_drop         → subtracts from total_in (cash removed from attendant)
  cash_pickup       → subtracts from total_in (cash removed from attendant)
  vendor_payment    → adds to total_out (attendant paid out cash)
  expense           → adds to total_out (attendant paid out cash)
"""

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class FMSAccountPaymentExtension(models.Model):
    """Adds FMS context fields to account.payment."""
    _inherit = 'account.payment'

    fms_shift_id = fields.Many2one(
        'fms.shift', 'FMS Shift',
        ondelete='restrict',
        index=True,
        copy=False,
        help="The shift during which this payment was made or received.",
    )
    fms_attendant_id = fields.Many2one(
        'hr.employee', 'Attendant',
        domain=[('fms_is_attendant', '=', True)],
        index=True,
        help="The attendant responsible for this cash transaction.",
    )
    fms_station_id = fields.Many2one(
        'res.company', 'Station',
        related='fms_shift_id.company_id', store=True, readonly=True,
        help="Derived from the shift — identifies which station this payment belongs to.",
    )
    fms_payment_context = fields.Selection([
        ('customer_receipt', 'Customer Receipt'),
        ('vendor_payment',   'Vendor Payment from Shift Cash'),
        ('cash_float',       'Cash Float'),
        ('cash_drop',        'Cash Drop / Pickup'),
        ('expense',          'Expense from Shift Cash'),
        ('other',            'Other Shift Cash Movement'),
    ], string='FMS Payment Context',
        help="Classifies the purpose of this payment within the shift cash reconciliation.",
        copy=False,
    )

    @api.constrains('fms_shift_id', 'company_id')
    def _check_fms_payment_company(self):
        for pay in self:
            if pay.fms_shift_id and pay.fms_shift_id.company_id != pay.company_id:
                raise ValidationError(
                    f"Payment company ({pay.company_id.name}) does not match "
                    f"shift company ({pay.fms_shift_id.company_id.name}). "
                    "A payment can only be linked to a shift from the same company."
                )

    @api.constrains('fms_shift_id', 'fms_payment_context')
    def _check_fms_shift_state(self):
        """Prevent linking payments to closed shifts."""
        for pay in self:
            if not pay.fms_shift_id or not pay.fms_payment_context:
                continue
            if pay.fms_shift_id.state == 'closed':
                raise ValidationError(
                    f"Cannot link a payment to closed shift '{pay.fms_shift_id.display_name}'. "
                    "Closed shifts are locked."
                )

    def _auto_init(self):
        super()._auto_init()
        # FIN-010: composite index for FIN-008 SQL view aggregation queries
        # (shift_id, attendant_id, payment_context, state) — covers all WHERE clauses
        # in _compute_from_payments and the R27 SQL view.
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS account_payment_fms_shift_context_idx
            ON account_payment (fms_shift_id, fms_attendant_id, fms_payment_context, state)
            WHERE fms_shift_id IS NOT NULL
        """)
