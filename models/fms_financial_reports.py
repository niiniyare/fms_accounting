"""
fms_financial_reports.py — P&L, Balance Sheet, Trial Balance wizards.

All three reports query account.move.line directly by account_type so
they are always complete regardless of third-party report addon configuration.

Account type groupings (Odoo 18):
  Income        : income, income_other
  COGS          : expense_direct_cost
  Expenses      : expense, expense_depreciation
  Current Assets: asset_cash, asset_current, asset_prepayments, asset_receivable
  Fixed Assets  : asset_fixed, asset_non_current
  Current Liab  : liability_payable, liability_current
  LT Liabilities: liability_non_current
  Equity        : equity, equity_unaffected
"""

from odoo import api, fields, models

# ── Constants ──────────────────────────────────────────────────────────────────

INCOME_TYPES      = ('income', 'income_other')
COGS_TYPES        = ('expense_direct_cost',)
EXPENSE_TYPES     = ('expense', 'expense_depreciation')
ASSET_CURRENT     = ('asset_cash', 'asset_current', 'asset_prepayments', 'asset_receivable')
ASSET_FIXED       = ('asset_fixed', 'asset_non_current')
LIAB_CURRENT      = ('liability_payable', 'liability_current')
LIAB_NONCURRENT   = ('liability_non_current',)
EQUITY_TYPES      = ('equity', 'equity_unaffected')
ALL_PL_TYPES      = INCOME_TYPES + COGS_TYPES + EXPENSE_TYPES


def _account_balances(env, account_types, date_from=None, date_to=None,
                      target_move='posted', company_id=None):
    """
    Return list of dicts {code, name, account_type, debit, credit, balance}
    for all accounts of the given types that have any movement in the period.

    balance = credit - debit  (positive = credit-normal; callers flip sign as needed)
    """
    domain = [
        ('account_id.account_type', 'in', account_types),
        ('display_type', 'not in', ('line_section', 'line_note')),
        ('parent_state', '=', 'posted' if target_move == 'posted' else 'draft'),
    ]
    if target_move == 'all':
        domain = [d for d in domain if d != ('parent_state', '=', 'draft')]
        domain.append(('parent_state', 'in', ('posted', 'draft')))

    if date_from:
        domain.append(('date', '>=', date_from))
    if date_to:
        domain.append(('date', '<=', date_to))
    if company_id:
        domain.append(('company_id', '=', company_id.id))

    lines = env['account.move.line'].read_group(
        domain,
        fields=['account_id', 'debit:sum', 'credit:sum'],
        groupby=['account_id'],
        orderby='account_id',
    )

    result = []
    for row in lines:
        acc_id, acc_name = row['account_id']
        acc = env['account.account'].browse(acc_id)
        debit  = row['debit']  or 0.0
        credit = row['credit'] or 0.0
        result.append({
            'id':           acc_id,
            'code':         acc.code or '',
            'name':         acc.name or acc_name,
            'account_type': acc.account_type,
            'debit':        debit,
            'credit':       credit,
            'balance':      credit - debit,
        })
    return result


def _section_total(rows):
    return sum(r['balance'] for r in rows)


# ═══════════════════════════════════════════════════════════════════════════════
# Profit & Loss
# ═══════════════════════════════════════════════════════════════════════════════

class FMSReportPL(models.TransientModel):
    _name        = 'fms.report.pl'
    _description = 'Profit & Loss Report'

    date_from    = fields.Date('From Date')
    date_to      = fields.Date('To Date',   default=fields.Date.today)
    target_move  = fields.Selection([
        ('posted', 'All Posted Entries'),
        ('all',    'All Entries (incl. draft)'),
    ], string='Target Moves', default='posted')
    company_id   = fields.Many2one('res.company', default=lambda s: s.env.company)

    # ── Action ─────────────────────────────────────────────────────────────────

    def action_print(self):
        return self.env.ref('fms_accounting.action_report_fms_pl').report_action(self)

    # ── Data builder ───────────────────────────────────────────────────────────

    def get_report_data(self):
        """Return structured P&L data for the QWeb template."""
        self.ensure_one()
        kw = dict(
            date_from   = self.date_from,
            date_to     = self.date_to,
            target_move = self.target_move,
            company_id  = self.company_id,
        )

        income_rows = _account_balances(self.env, INCOME_TYPES, **kw)
        cogs_rows   = _account_balances(self.env, COGS_TYPES,   **kw)
        expense_rows= _account_balances(self.env, EXPENSE_TYPES, **kw)

        # For income: positive balance (CR > DR) = revenue earned
        # For costs:  negative balance (DR > CR) = cost incurred → flip sign for display
        total_income      = _section_total(income_rows)
        total_cogs        = -_section_total(cogs_rows)     # flip: cost is DR-normal
        gross_profit      = total_income - total_cogs
        total_expenses    = -_section_total(expense_rows)  # flip
        net_profit        = gross_profit - total_expenses

        # Flip cost/expense rows so positive = cost
        for r in cogs_rows + expense_rows:
            r['balance'] = -r['balance']

        return {
            'wizard':         self,
            'income_rows':    income_rows,
            'cogs_rows':      cogs_rows,
            'expense_rows':   expense_rows,
            'total_income':   total_income,
            'total_cogs':     total_cogs,
            'gross_profit':   gross_profit,
            'total_expenses': total_expenses,
            'net_profit':     net_profit,
            'currency':       self.company_id.currency_id,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Balance Sheet
# ═══════════════════════════════════════════════════════════════════════════════

class FMSReportBalanceSheet(models.TransientModel):
    _name        = 'fms.report.balance.sheet'
    _description = 'Balance Sheet Report'

    date_to     = fields.Date('As of Date', default=fields.Date.today)
    target_move = fields.Selection([
        ('posted', 'All Posted Entries'),
        ('all',    'All Entries (incl. draft)'),
    ], string='Target Moves', default='posted')
    company_id  = fields.Many2one('res.company', default=lambda s: s.env.company)

    def action_print(self):
        return self.env.ref('fms_accounting.action_report_fms_balance_sheet').report_action(self)

    def get_report_data(self):
        self.ensure_one()
        kw = dict(date_to=self.date_to, target_move=self.target_move,
                  company_id=self.company_id)

        # Balance sheet is cumulative — no date_from
        asset_cur_rows  = _account_balances(self.env, ASSET_CURRENT,   **kw)
        asset_fix_rows  = _account_balances(self.env, ASSET_FIXED,     **kw)
        liab_cur_rows   = _account_balances(self.env, LIAB_CURRENT,    **kw)
        liab_lt_rows    = _account_balances(self.env, LIAB_NONCURRENT, **kw)
        equity_rows     = _account_balances(self.env, EQUITY_TYPES,    **kw)

        # Assets: DR-normal → flip balance so positive = asset value
        for r in asset_cur_rows + asset_fix_rows:
            r['balance'] = -r['balance']

        # Current-year P&L (income - expenses since fiscal year start)
        fy_start = self.company_id.fiscalyear_last_month
        # Simple: use Jan 1 of the date_to year as fiscal year start
        fy_date_from = self.date_to.replace(month=1, day=1) if self.date_to else None
        pl_kw = dict(date_from=fy_date_from, date_to=self.date_to,
                     target_move=self.target_move, company_id=self.company_id)
        income_rows2  = _account_balances(self.env, INCOME_TYPES,  **pl_kw)
        cogs_rows2    = _account_balances(self.env, COGS_TYPES,    **pl_kw)
        expense_rows2 = _account_balances(self.env, EXPENSE_TYPES, **pl_kw)
        current_year_profit = (
            _section_total(income_rows2)
            - (-_section_total(cogs_rows2))
            - (-_section_total(expense_rows2))
        ) * -1  # liabilities side: profit is credit-normal

        total_assets       = _section_total(asset_cur_rows) + _section_total(asset_fix_rows)
        total_liab_cur     = _section_total(liab_cur_rows)
        total_liab_lt      = _section_total(liab_lt_rows)
        total_equity       = _section_total(equity_rows) + current_year_profit
        total_liab_equity  = total_liab_cur + total_liab_lt + total_equity

        return {
            'wizard':               self,
            'asset_cur_rows':       asset_cur_rows,
            'asset_fix_rows':       asset_fix_rows,
            'liab_cur_rows':        liab_cur_rows,
            'liab_lt_rows':         liab_lt_rows,
            'equity_rows':          equity_rows,
            'current_year_profit':  current_year_profit,
            'total_assets':         total_assets,
            'total_asset_current':  _section_total(asset_cur_rows),
            'total_asset_fixed':    _section_total(asset_fix_rows),
            'total_liab_cur':       total_liab_cur,
            'total_liab_lt':        total_liab_lt,
            'total_equity':         total_equity,
            'total_liab_equity':    total_liab_equity,
            'balanced':             abs(total_assets - total_liab_equity) < 0.02,
            'currency':             self.company_id.currency_id,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Trial Balance
# ═══════════════════════════════════════════════════════════════════════════════

ALL_TYPES = (
    ASSET_CURRENT + ASSET_FIXED +
    LIAB_CURRENT + LIAB_NONCURRENT +
    EQUITY_TYPES +
    INCOME_TYPES + COGS_TYPES + EXPENSE_TYPES
)


class FMSReportTrialBalance(models.TransientModel):
    _name        = 'fms.report.trial.balance'
    _description = 'Trial Balance Report'

    date_from        = fields.Date('From Date', default=lambda s: s._default_date_from())
    date_to          = fields.Date('To Date',   default=fields.Date.today)
    target_move      = fields.Selection([
        ('posted', 'All Posted Entries'),
        ('all',    'All Entries (incl. draft)'),
    ], string='Target Moves', default='posted')
    company_id       = fields.Many2one('res.company', default=lambda s: s.env.company)
    hide_zero        = fields.Boolean('Hide Zero-Balance Accounts', default=True)

    def _default_date_from(self):
        today = fields.Date.today()
        return today.replace(day=1)

    def action_print(self):
        return self.env.ref('fms_accounting.action_report_fms_trial_balance').report_action(self)

    def get_report_data(self):
        self.ensure_one()
        company_id = self.company_id

        # Opening balances: everything before date_from
        opening_kw = dict(date_to=self.date_from, target_move=self.target_move,
                          company_id=company_id)
        opening_rows = _account_balances(self.env, ALL_TYPES, **opening_kw)
        opening_by_id = {r['id']: r['balance'] for r in opening_rows}

        # Period movements
        period_kw = dict(date_from=self.date_from, date_to=self.date_to,
                         target_move=self.target_move, company_id=company_id)
        period_rows = _account_balances(self.env, ALL_TYPES, **period_kw)
        period_by_id = {r['id']: r for r in period_rows}

        # All account ids seen in either set
        all_ids = set(opening_by_id) | set(period_by_id)
        # Also get metadata for opening-only accounts
        acc_meta = {}
        if all_ids:
            for acc in self.env['account.account'].browse(list(all_ids)):
                acc_meta[acc.id] = {
                    'id': acc.id, 'code': acc.code or '',
                    'name': acc.name, 'account_type': acc.account_type,
                }

        rows = []
        for aid in sorted(all_ids, key=lambda i: acc_meta.get(i, {}).get('code', '')):
            meta    = acc_meta.get(aid, {})
            opening = opening_by_id.get(aid, 0.0)
            p       = period_by_id.get(aid, {})
            debit   = p.get('debit', 0.0)
            credit  = p.get('credit', 0.0)
            closing = opening + credit - debit
            if self.hide_zero and abs(opening) < 0.01 and abs(debit) < 0.01 \
                    and abs(credit) < 0.01 and abs(closing) < 0.01:
                continue
            rows.append({
                'code':    meta.get('code', ''),
                'name':    meta.get('name', ''),
                'opening': opening,
                'debit':   debit,
                'credit':  credit,
                'closing': closing,
            })

        total_opening = sum(r['opening'] for r in rows)
        total_debit   = sum(r['debit']   for r in rows)
        total_credit  = sum(r['credit']  for r in rows)
        total_closing = sum(r['closing'] for r in rows)

        return {
            'wizard':        self,
            'rows':          rows,
            'total_opening': total_opening,
            'total_debit':   total_debit,
            'total_credit':  total_credit,
            'total_closing': total_closing,
            'balanced':      abs(total_debit - total_credit) < 0.02,
            'currency':      company_id.currency_id,
        }
