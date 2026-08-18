"""
fms_fuel_delivery.py — Fuel tanker delivery recording

Workflow: draft → confirmed → billed

On confirm:
  - Creates one stock.picking (incoming) per delivery with one move per line
  - Writes fms.dip_log records with dip_type='offloading' for wetstock audit
  - Creates one account.move (vendor bill) with one line per delivery product

dip_type on fms.dip_log distinguishes two loss categories:
  'shift_close'  — shift-end dip variance (evaporation, meter drift)
  'offloading'   — delivery dip variance (short delivery, transfer loss)
"""

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class FMSFuelDelivery(models.Model):
    _name = 'fms.fuel.delivery'
    _description = 'Fuel Delivery'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'delivery_date desc, name desc'

    name = fields.Char('Reference', readonly=True, default='New', copy=False)
    delivery_date = fields.Date('Delivery Date', required=True, default=fields.Date.today)
    tanker_ref = fields.Char('Tanker / Delivery Note Ref')
    vendor_id = fields.Many2one('res.partner', 'Supplier', required=True,
                                domain=[('supplier_rank', '>', 0)])
    company_id = fields.Many2one('res.company', required=True,
                                 default=lambda self: self.env.company)

    delivery_line_ids = fields.One2many('fms.fuel.delivery.line', 'delivery_id', 'Lines')

    stock_picking_id = fields.Many2one('stock.picking', 'Stock Receipt', readonly=True, copy=False)
    vendor_bill_id = fields.Many2one('account.move', 'Vendor Bill', readonly=True, copy=False)

    state = fields.Selection([
        ('draft',     'Draft'),
        ('confirmed', 'Confirmed'),
        ('billed',    'Billed'),
    ], default='draft', readonly=True, copy=False)

    notes = fields.Text('Notes')

    # ------------------------------------------------------------------
    # Totals
    # ------------------------------------------------------------------

    total_litres = fields.Float('Total Litres', compute='_compute_totals', digits=(16, 2))
    total_amount = fields.Float('Total Amount', compute='_compute_totals', digits=(16, 2))

    @api.depends('delivery_line_ids.quantity_litres', 'delivery_line_ids.unit_price')
    def _compute_totals(self):
        for rec in self:
            rec.total_litres = sum(rec.delivery_line_ids.mapped('quantity_litres'))
            rec.total_amount = sum(
                l.quantity_litres * l.unit_price for l in rec.delivery_line_ids
            )

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('fms.fuel.delivery') or 'DEL/???'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_confirm(self):
        self.ensure_one()
        if self.state != 'draft':
            raise ValidationError("Only draft deliveries can be confirmed.")
        if not self.delivery_line_ids:
            raise ValidationError("Add at least one product line before confirming.")

        with self.env.cr.savepoint():
            picking = self._create_stock_picking()
            self._create_offloading_dip_logs()
            bill = self._create_vendor_bill()

        self.write({
            'state': 'confirmed',
            'stock_picking_id': picking.id,
            'vendor_bill_id': bill.id,
        })

    def action_reset_draft(self):
        self.ensure_one()
        if self.state == 'billed':
            raise ValidationError("Cannot reset a billed delivery.")
        self.write({'state': 'draft'})

    def action_view_bill(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.vendor_bill_id.id,
            'view_mode': 'form',
        }

    def action_view_picking(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': self.stock_picking_id.id,
            'view_mode': 'form',
        }

    # ------------------------------------------------------------------
    # Internal: stock receipt
    # ------------------------------------------------------------------

    def _create_stock_picking(self):
        self.ensure_one()
        wh = self.env['stock.warehouse'].search(
            [('company_id', '=', self.company_id.id)], limit=1
        )
        if not wh:
            raise ValidationError("No warehouse found for this company.")

        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'incoming'),
            ('warehouse_id', '=', wh.id),
        ], limit=1)
        if not picking_type:
            raise ValidationError("No incoming picking type found for the warehouse.")

        moves = []
        for line in self.delivery_line_ids:
            if not line.product_id or line.quantity_litres <= 0:
                continue
            moves.append((0, 0, {
                'name': line.product_id.name,
                'product_id': line.product_id.id,
                'product_uom_qty': line.quantity_litres,
                'product_uom': line.product_id.uom_id.id,
                'location_id': picking_type.default_location_src_id.id,
                'location_dest_id': line.location_id.id,
            }))

        picking = self.env['stock.picking'].create({
            'partner_id': self.vendor_id.id,
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': wh.lot_stock_id.id,
            'origin': self.name,
            'move_ids': moves,
        })
        picking.action_confirm()
        return picking

    # ------------------------------------------------------------------
    # Internal: offloading dip logs
    # ------------------------------------------------------------------

    def _create_offloading_dip_logs(self):
        self.ensure_one()
        DipLog = self.env['fms.dip_log'].sudo()
        for line in self.delivery_line_ids:
            if not line.location_id:
                continue
            DipLog.create({
                'shift_id': False,
                'location_id': line.location_id.id,
                'product_id': line.product_id.id,
                'opening_volume': line.dip_before,
                'closing_volume': line.dip_after,
                'dip_type': 'offloading',
                'delivery_line_id': line.id,
            })

    # ------------------------------------------------------------------
    # Internal: vendor bill
    # ------------------------------------------------------------------

    def _create_vendor_bill(self):
        self.ensure_one()
        bill_lines = []
        for line in self.delivery_line_ids:
            if line.quantity_litres <= 0:
                continue
            account = (
                line.product_id.fms_cogs_account_id
                or line.product_id.product_tmpl_id.get_product_accounts().get('expense')
            )
            if not account:
                raise ValidationError(
                    f"Product '{line.product_id.name}' has no COGS account. "
                    "Set fms_cogs_account_id on the product."
                )
            bill_lines.append((0, 0, {
                'product_id': line.product_id.id,
                'name': f"{line.product_id.name} — {self.name}",
                'quantity': line.quantity_litres,
                'price_unit': line.unit_price,
                'account_id': account.id,
            }))

        bill = self.env['account.move'].sudo().create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor_id.id,
            'invoice_date': self.delivery_date,
            'ref': self.tanker_ref or self.name,
            'invoice_line_ids': bill_lines,
        })
        return bill


class FMSFuelDeliveryLine(models.Model):
    _name = 'fms.fuel.delivery.line'
    _description = 'Fuel Delivery Line'
    _order = 'delivery_id, sequence'

    delivery_id = fields.Many2one('fms.fuel.delivery', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)

    product_id = fields.Many2one(
        'product.product', 'Product', required=True,
        domain=[('fms_is_fuel', '=', True)],
    )
    location_id = fields.Many2one(
        'stock.location', 'Destination Tank', required=True,
        domain=[('fms_is_fuel_tank', '=', True)],
    )
    quantity_litres = fields.Float('Invoiced Qty (L)', digits=(16, 2))
    unit_price = fields.Float('Unit Price (/L)', digits=(16, 4))

    # ── Dip verification ─────────────────────────────────────────────────────
    dip_before = fields.Float(
        'Dip Before Offloading (L)', digits=(16, 2),
        help="Tank dip reading immediately before the tanker starts offloading.",
    )
    sales_during = fields.Float(
        'Sales During Offloading (L)', digits=(16, 2),
        help="Litres sold from this tank while the tanker was still connected.",
    )
    expected_qty = fields.Float(
        'Expected Dip After (L)', compute='_compute_dip', store=True, digits=(16, 2),
        help="dip_before + quantity_litres - sales_during",
    )
    dip_after = fields.Float(
        'Dip After Offloading (L)', digits=(16, 2),
        help="Tank dip reading after tanker disconnects and product settles.",
    )
    variance = fields.Float(
        'Variance (L)', compute='_compute_dip', store=True, digits=(16, 2),
        help="dip_after - expected_qty. Negative = short delivery / transfer loss.",
    )

    @api.depends('dip_before', 'quantity_litres', 'sales_during', 'dip_after')
    def _compute_dip(self):
        for line in self:
            line.expected_qty = line.dip_before + line.quantity_litres - line.sales_during
            line.variance = line.dip_after - line.expected_qty

    subtotal = fields.Float('Subtotal', compute='_compute_subtotal', digits=(16, 2))

    @api.depends('quantity_litres', 'unit_price')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity_litres * line.unit_price


# ---------------------------------------------------------------------------
# Extend fms.dip_log with dip_type and delivery_line_id
# ---------------------------------------------------------------------------

class FMSDipLogAccounting(models.Model):
    _inherit = 'fms.dip_log'

    # Relax required=True so offloading dip logs can exist without a shift
    shift_id = fields.Many2one('fms.shift', 'Shift', required=False, ondelete='cascade')

    dip_type = fields.Selection([
        ('shift_close', 'Shift Close'),
        ('offloading',  'Delivery Offloading'),
    ], string='Dip Type', default='shift_close', index=True,
       help="shift_close: normal end-of-shift dip (evaporation / meter drift). "
            "offloading: dip taken during a tanker delivery (transfer loss / short delivery).")

    delivery_line_id = fields.Many2one(
        'fms.fuel.delivery.line', 'Delivery Line', readonly=True,
        help="Set when this log was created by a fuel delivery confirmation.",
    )
