"""
fms_credit_customer.py — Credit invoice extensions + credit limit enforcement

account.move  : vehicle, driver, limit-bypass fields
account.payment: PDC tracking
res.partner   : credit limit, on-hold flag, live exposure compute
"""

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError


class ResPartnerCreditLimit(models.Model):
    _inherit = 'res.partner'

    fms_is_fleet_customer = fields.Boolean(
        'Fleet Customer', default=False,
        help="Mark this partner as a fleet account holder eligible for credit fuel purchases.",
    )
    fms_credit_limit   = fields.Float(
        'FMS Credit Limit', default=0.0,
        help="Maximum outstanding balance allowed. 0 = no limit enforced.",
    )
    fms_on_hold        = fields.Boolean(
        'Account on Hold', default=False,
        help="Block all new credit sales — no supervisor override available.",
    )
    fms_credit_exposure = fields.Float(
        'Current Exposure', compute='_compute_credit_exposure', store=False,
        help="Posted AR + unposted customer invoices. Used by the credit limit check.",
    )
    fms_exposure_pct   = fields.Float(
        'Exposure %', compute='_compute_credit_exposure', store=False,
    )

    def _compute_credit_exposure(self):
        MoveLine = self.env['account.move.line']
        Move     = self.env['account.move']
        for partner in self:
            # Posted AR balance: open receivable lines
            posted = sum(
                MoveLine.search([
                    ('partner_id', '=', partner.id),
                    ('account_id.account_type', '=', 'asset_receivable'),
                    ('reconciled', '=', False),
                    ('parent_state', '=', 'posted'),
                ]).mapped('amount_residual')
            )
            # Unposted (draft) customer invoices not yet confirmed
            unposted = sum(
                Move.search([
                    ('partner_id', '=', partner.id),
                    ('move_type', '=', 'out_invoice'),
                    ('state', '=', 'draft'),
                ]).mapped('amount_total')
            )
            exposure = posted + unposted
            partner.fms_credit_exposure = exposure
            if partner.fms_credit_limit > 0:
                partner.fms_exposure_pct = (exposure / partner.fms_credit_limit) * 100
            else:
                partner.fms_exposure_pct = 0.0


class AccountMoveVehicle(models.Model):
    _inherit = 'account.move'

    fms_vehicle = fields.Char(
        'Vehicle / Plate',
        help="Vehicle registration or plate number for fleet credit invoices.",
    )
    fms_driver = fields.Many2one(
        'hr.employee', 'Driver (Employee)',
        help="Driver or authorised person for this credit transaction (legacy: employee link).",
    )

    # Credit limit bypass — only a supervisor should set this
    fms_limit_bypass       = fields.Boolean(
        'Credit Limit Override', default=False, copy=False,
        help="Supervisor authorisation to post despite the customer being over limit.",
    )
    fms_limit_bypass_reason = fields.Char(
        'Override Reason', copy=False,
        help="Mandatory reason when bypassing the credit limit.",
    )
    fms_limit_bypass_uid   = fields.Many2one(
        'res.users', 'Override By', readonly=True, copy=False,
    )

    @api.onchange('partner_id')
    def _onchange_partner_credit_check(self):
        """Warn immediately when a partner is selected who is near or over limit."""
        if not self.partner_id or self.move_type != 'out_invoice':
            return
        partner = self.partner_id
        if partner.fms_on_hold:
            return {
                'warning': {
                    'title': 'Account on Hold',
                    'message': (
                        f"{partner.name} is on hold. No credit sales are allowed. "
                        "Contact the supervisor."
                    ),
                }
            }
        if partner.fms_credit_limit > 0:
            exposure = partner.fms_credit_exposure
            pct = (exposure / partner.fms_credit_limit) * 100
            if pct >= 100:
                return {
                    'warning': {
                        'title': 'Over Credit Limit',
                        'message': (
                            f"{partner.name} is over their credit limit.\n"
                            f"Limit: {self.company_id.currency_id.name} {partner.fms_credit_limit:,.2f}\n"
                            f"Exposure: {self.company_id.currency_id.name} {exposure:,.2f} ({pct:.1f}%)\n\n"
                            "A supervisor override with reason is required to post."
                        ),
                    }
                }
            elif pct >= 90:
                return {
                    'warning': {
                        'title': 'Approaching Credit Limit',
                        'message': (
                            f"{partner.name} is at {pct:.1f}% of their credit limit "
                            f"({self.company_id.currency_id.name} {exposure:,.2f} of {self.company_id.currency_id.name} {partner.fms_credit_limit:,.2f})."
                        ),
                    }
                }

    def action_post(self):
        """Block posting if partner is on hold or over limit without supervisor bypass."""
        for move in self.filtered(lambda m: m.move_type == 'out_invoice'):
            partner = move.partner_id
            if not partner:
                continue

            if partner.fms_on_hold:
                raise UserError(
                    f"Cannot post: {partner.name} is on hold. "
                    "Contact the supervisor to lift the hold before creating new credit sales."
                )

            if partner.fms_credit_limit > 0:
                # Exposure excludes this draft invoice (it's the one being posted)
                posted = sum(
                    self.env['account.move.line'].search([
                        ('partner_id', '=', partner.id),
                        ('account_id.account_type', '=', 'asset_receivable'),
                        ('reconciled', '=', False),
                        ('parent_state', '=', 'posted'),
                    ]).mapped('amount_residual')
                )
                other_drafts = sum(
                    self.env['account.move'].search([
                        ('partner_id', '=', partner.id),
                        ('move_type', '=', 'out_invoice'),
                        ('state', '=', 'draft'),
                        ('id', '!=', move.id),
                    ]).mapped('amount_total')
                )
                exposure_after = posted + other_drafts + move.amount_total
                if exposure_after > partner.fms_credit_limit:
                    if not move.fms_limit_bypass:
                        raise UserError(
                            f"Credit limit exceeded for {partner.name}.\n"
                            f"Limit: {move.company_id.currency_id.name} {partner.fms_credit_limit:,.2f}\n"
                            f"Exposure after this invoice: {move.company_id.currency_id.name} {exposure_after:,.2f}\n\n"
                            "To override, tick 'Credit Limit Override' and enter a reason."
                        )
                    if not move.fms_limit_bypass_reason:
                        raise ValidationError(
                            "An override reason is required when bypassing the credit limit."
                        )
                    # Record who authorised the bypass
                    move.fms_limit_bypass_uid = self.env.user.id

        return super().action_post()


class AccountPaymentPDC(models.Model):
    _inherit = 'account.payment'

    fms_is_pdc = fields.Boolean(
        'Post-Dated Cheque', default=False,
    )
    fms_pdc_state = fields.Selection([
        ('held',    'Held'),
        ('cleared', 'Cleared'),
        ('bounced', 'Bounced'),
    ], string='PDC Status')
    fms_cheque_number = fields.Char('Cheque Number')
    fms_cheque_date   = fields.Date('Cheque Date')
