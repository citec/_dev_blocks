# -*- coding: utf-8 -*-

#############################################################
# IMPORTS
#############################################################
from openerp.osv import fields, osv, orm
from openerp.tools.translate import _
import openerp.addons.decimal_precision as dp
import openerp.addons.l10n_br_base.tools.fiscal as fiscal
from openerp import netsvc
from EANBarCode import EanBarCode
import StringIO
import cStringIO
import time
import re
import pycurl
import urllib
from lxml import etree
import logging

_logger = logging.getLogger(__name__)

#############################################################
# Class declaration
#############################################################
class event_speaker(osv.osv):
    _name = 'event.speaker'
    _description = 'Fuel log for vehicles'
    # Inheritance
    _inherit = 'event.event'
    _inherit = ['event.registration', 'mail.thread', 'ir.needaction_mixin']
    _inherits = {'fleet.vehicle.cost': 'cost_id'}
    _order = 'date_start ASC'

#############################################################
# Field types
#############################################################
    _columns = {
        # Text fields
        'inv_ref': fields.char('Invoice Reference', size=64),
        'notes': fields.text('Notes'),
        # Numbers
        'sequence': fields.integer('Sequence', ),
        'liter': fields.float('Liter'),
        # Relations
        'purchaser_id': fields.many2one('res.partner', 'Purchaser', domain="['|',('customer','=',True),('employee','=',True)]"),
        # Related
        'cost_amount': fields.related('cost_id', 'amount', string='Amount', type='float', store=True),
        'image': fields.related('partner_id', 'image', type='binary', string='Picture'),
        # Functions
        'move_lines':fields.function(_get_lines, type='many2many', relation='account.move.line', string='Entry Lines'),
        'amount_total': fields.function(_amount_all, digits_compute=dp.get_precision('Account'), string='Total',
            store={
                'account.invoice': (lambda self, cr, uid, ids, c={}: ids, ['invoice_line'], 20),
                'account.invoice.tax': (_get_invoice_tax, None, 20),
                'account.invoice.line': (_get_invoice_line, ['price_unit','invoice_line_tax_id','quantity','discount','invoice_id'], 20),
            },
            multi='all'),
        # Others
        'partner_id': fields.many2one('res.partner', 'Speaker', select=True, ondelete='cascade'),
        'date_start': fields.datetime('Start Date/Time', ),
        'date_end': fields.datetime('End Date/Time', ),
        'speaker_confirmed': fields.boolean('Confirmed', readonly=False, ),
    }

#############################################################
# Defaults
#############################################################
    _defaults = {
        # Literals
        'sequence': 1,
        'yesornot': True,
        'cost_type': 'fuel',
        # Functions
        'cost_subtype_id': _get_default_service_type,
    }

#############################################################
# on_change functions
#############################################################
    def on_change_vehicle(self, cr, uid, ids, vehicle_id, context=None):
        if not vehicle_id:
            return {}
        if not context:
            context = {}
        vehicle = self.pool.get('fleet.vehicle').browse(cr, uid, vehicle_id, context=context)
        odometer_unit = vehicle.odometer_unit
        driver = vehicle.driver_id.id
        return {
            'value': {
                'odometer_unit': odometer_unit,
                'purchaser_id': driver,
            }
        }

#############################################################
# default functions
#############################################################

    def _get_default_service_type(self, cr, uid, context):
        try:
            model, model_id = self.pool.get('ir.model.data').get_object_reference(cr, uid, 'fleet', 'type_service_service_8')
        except ValueError:
            model_id = False
        return model_id

#############################################################
# function fields functions
#############################################################
    # without multi
    def _get_lines(self, cr, uid, ids, name, arg, context=None):
        res = {}
        for invoice in self.browse(cr, uid, ids, context=context):
            id = invoice.id
            res[id] = []
            if not invoice.move_id:
                continue
            data_lines = [x for x in invoice.move_id.line_id if x.account_id.id == invoice.account_id.id]
            partial_ids = []
            for line in data_lines:
                ids_line = []
                if line.reconcile_id:
                    ids_line = line.reconcile_id.line_id
                elif line.reconcile_partial_id:
                    ids_line = line.reconcile_partial_id.line_partial_ids
                l = map(lambda x: x.id, ids_line)
                partial_ids.append(line.id)
                res[id] =[x for x in l if x <> line.id and x not in partial_ids]
        return res

    # with multi
    def _occupancy(self, cr, uid, ids, name, args, context=None):
        result = {}
        for obj in self.browse(cr, uid, ids, context=context):
            # Occupancy
            occupancy = len(self.pool.get('event.registration.line').search(cr, uid, [('room_id', 'in', [obj.id])]))
            # Occupancy Rate
            rate = obj.max_capacity and (100.0 * occupancy / obj.max_capacity) or 0.00
            result[obj.id] = {  'occupancy': occupancy,
                                'occupancy_rate': rate,
                                }
        return result

    # with multi
    def _amount_all(self, cr, uid, ids, name, args, context=None):
        res = {}
        for invoice in self.browse(cr, uid, ids, context=context):
            res[invoice.id] = {
                'amount_untaxed': 0.0,
                'amount_tax': 0.0,
                'amount_total': 0.0
            }
            for line in invoice.invoice_line:
                res[invoice.id]['amount_untaxed'] += line.price_subtotal
            for line in invoice.tax_line:
                res[invoice.id]['amount_tax'] += line.amount
            res[invoice.id]['amount_total'] = res[invoice.id]['amount_tax'] + res[invoice.id]['amount_untaxed']
        return res

#############################################################
# workflow functions
#############################################################

    _columns = {
        'name': fields.char('Name', size=64, required=True),
        'max_capacity': fields.integer('Max. Capacity', ),
        'image': fields.binary("Image", ),
        'occupancy': fields.function(_occupancy, multi="occupancy", type="integer", string="Occupancy"),
        # Fields only for usability pourpose
        'occupancy_rate': fields.function(_occupancy, multi="occupancy", type="integer", string="Occupancy Rate"),
    }

    _order = 'sequence ASC'

class event_event(osv.osv):
    _name = 'event.event'
    _inherit = 'event.event'

    def button_import_scb_registration(self, cr, uid, ids, context=None):
        self.import_scb_registration(cr, uid, context=None)
        return True

    def import_scb_registration(self, cr, uid, context=None):
        if not context:
            context = {}
        update = False
        dbsource_id = self.pool.get("base.external.dbsource").search(cr, uid, [('name', '=', 'SCB - Web')])
        if isinstance(dbsource_id, (int, long)):
            dbsource_id = [dbsource_id]
        dbsource_brw = dbsource_id and self.pool.get("base.external.dbsource").browse(cr, uid, dbsource_id, context) or None
        if dbsource_brw:
            dbsource_brw = dbsource_brw[0]
            sqlQuery = "select * from p2nyd_facileforms_records where form=22 OR form=27"
            rows = self.pool.get("base.external.dbsource").execute(cr, uid, dbsource_id, sqlQuery, sqlparams={'name1': 'blabla', 'name2': 'blabla'}, context=context)
            for row in rows:
                rowID = row[0]
                already_imported = self.pool.get("event.registration").search(cr, uid, ['&', ('imported_id', '=', rowID), ('imported_dbsource_id', 'in', dbsource_id)])
                if already_imported:
                    if not update:
                        continue
                sqlQuery2 = "select * from p2nyd_facileforms_subrecords where record=%d" % rowID
                subrecords = self.pool.get("base.external.dbsource").execute(cr, uid, dbsource_id, sqlQuery2, sqlparams={'name1': 'blabla', 'name2': 'blabla'}, context=context)
                registration = {}
                for subrecord in subrecords:
                    registration.update({subrecord[4]: subrecord[6]})

                partner_id = self.searchAndCreateIfNeeded(cr, uid, registration, type='partner', partner_id=None, responsible_id=None, update=update, dbsource_id=dbsource_id, rowID=rowID, context=context)
                if not partner_id:
                    continue

                responsible_id = self.searchAndCreateIfNeeded(cr, uid, registration, type='responsible', partner_id=partner_id, responsible_id=None, update=update, dbsource_id=dbsource_id, rowID=rowID, context=context)
                if not responsible_id: # Si foi marcado mas os dados não foram suficientes
                    responsible_id = partner_id

                invoice_partner_id = self.searchAndCreateIfNeeded(cr, uid, registration, type='responsible', partner_id=partner_id, responsible_id=None, update=update, dbsource_id=dbsource_id, rowID=rowID, context=context)
                if not invoice_partner_id:
                    invoice_partner_id = partner_id

                # Do we need to create a company where the partner belongs to ?
                if 'empresa_text' in registration and registration['empresa_text'] or False:
                    partner_brw = self.pool.get("res.partner").browse(cr, uid, partner_id, context)
                    if not partner_brw.is_company: # Crear a empresa
                        company_id =  self.pool.get("res.partner").search(cr, uid, ['&', '&', ('is_company', '=', True), ('name', '=', registration['empresa_text']), ('imported_dbsource_id', 'in', dbsource_id)])
                        vals = {'is_company': True,
                                'to_review': True,
                                'name': registration['empresa_text'],
                                'child_ids': [  (4, partner_id),
                                                (4, responsible_id),
                                                (4, invoice_partner_id)],
                                'imported_dbsource_id': isinstance(dbsource_id, (int, long)) and dbsource_id or dbsource_id[0],
                                'imported_reg_id': rowID,
                                }
                        if not company_id:
                            company_id = self.pool.get("res.partner").create(cr, uid, vals, context)
                        elif update:
                            company_id = self.pool.get("res.partner").write(cr, uid, company_id, vals, context)

                # USE Cases
                rg_partner_id = invoice_partner_id
                vals = {}
                # - nota_participante_check != sim and nota_responsavel_check!=sim
                #   write inscr_estadual && write inscr_mun to invoice_partner_id
                if 'nota_participante_check' not in registration or registration['nota_participante_check']!='sim':
                    if 'nota_responsavel_check' not in registration or registration['nota_responsavel_check']!='sim':
                        pass

                # - nota_participante_check == sim
                #   invoice_partner_id
                #   if partner_id.is_company or not invoice_partner_id.parent_id:
                #       write inscr_estadual && write inscr_mun to invoice_partner_id
                #   else:
                #       write inscr_estadual && write inscr_mun to invoice_partner_id.parent_id
                elif 'nota_participante_check' in registration and registration['nota_participante_check']=='sim':
                    invoice_partner_brw = self.pool.get("res.partner").browse(cr, uid, invoice_partner_id, context)
                    if invoice_partner_brw.is_company or not invoice_partner_brw.parent_id:
                        pass
                    else:
                        rg_partner_id = invoice_partner_brw.parent_id.id

                # - nota_responsavel_check == sim
                #   invoice_partner_id
                #   write inscr_estadual && write inscr_mun to invoice_partner_id
                elif 'nota_responsavel_check' in registration and registration['nota_responsavel_check']=='sim':
                    pass
                else:
                    raise osv.except_osv(_('Error!'),_("Se ha producido alguna situación con la que no contábamos!") % (type, ))

                # RG ou Inscr. Estadual:
                if 'rg_inscr_estadual_text' in registration and registration['rg_inscr_estadual_text']!='' or False:
                    to_review = False
                    rg_partner_brw = self.pool.get("res.partner").browse(cr, uid, rg_partner_id, context)
                    uf = rg_partner_brw.state_id and rg_partner_brw.state_id.code.lower() or ''
                    try:
                        mod = __import__('openerp.addons.l10n_br_base.tools.fiscal', globals(), locals(), 'fiscal')
                        validate = getattr(mod, 'validate_ie_%s' % uf)
                        if not validate(registration['rg_inscr_estadual_text']):
                            to_review = True
                    except AttributeError:
                        if not fiscal.validate_ie_param(uf, registration['rg_inscr_estadual_text']):
                            to_review = True
                    if to_review:
                        # es isento?
                        if registration['rg_inscr_estadual_text'].lower()=='isento':
                            pass
                        else:
                            vals = {'to_review': to_review,
                                    'inscr_est_invalido': registration['rg_inscr_estadual_text'],
                                    }
                    else:
                        vals = {'inscr_est': registration['rg_inscr_estadual_text']}
                else:
                    vals = {'to_review': True}

                # Inscr. Municipal
                if 'inscr_municipal_text' in registration and registration['inscr_municipal_text']!='' or False:
                    vals.update({'inscr_mun': registration['inscr_municipal_text']})

                # Finally write vals to partner
                if vals:
                    self.pool.get("res.partner").write(cr, uid, rg_partner_id, vals, context)

                # Create the inscription
                event_id = context.get('event_id', False) or dbsource_brw.event_id and dbsource_brw.event_id.id or self.pool.get("event.event").search(cr, uid, [('name', 'ilike', 'SmartCity Business')]) 
                if event_id:
                    if not isinstance(event_id, (int, long)):
                        event_id = event_id[0]
                    partner_brw = self.pool.get("res.partner").browse(cr, uid, partner_id, context)
                    responsible_brw = self.pool.get("res.partner").browse(cr, uid, responsible_id, context)
                    badge_name = 'cracha_text' in registration and registration['cracha_text']!='' and registration['cracha_text'] or partner_brw.name
                    badge_company_name = 'empresa_cracha_text' in registration and registration['empresa_cracha_text']!='' and registration['empresa_cracha_text'] or partner_brw.parent_id and partner_brw.parent_id.name or partner_brw.name
                    # Classe
                    registration_type_id = None
                    classe = 'investimento_radio' in registration and registration['investimento_radio'] or None
                    if classe:
                        registration_type_id = self.pool.get("event.registration.type").search(cr, uid, ['&', ('event_id', '=', event_id), ('form_class_value', '=', classe)])
                    else:
                        registration_type_id = self.pool.get("event.registration.type").search(cr, uid, ['&', ('event_id', '=', event_id), ('name', '=', 'PARTICIPANTE (Outros)')])
                    registration_type_id = not isinstance(registration_type_id, (int, long)) and registration_type_id[0] or registration_type_id
                    # Precio
                    registration_type_brw = self.pool.get("event.registration.type").browse(cr, uid, registration_type_id, context)
                    # Access group
                    access_group_id = registration_type_brw.access_group_id and registration_type_brw.access_group_id.id or None
                    price = registration_type_brw.price and isinstance(registration_type_brw.price, (list, tuple)) and registration_type_brw.price[0] or registration_type_brw.price or 0.0
                    imported_dbsource_id = not isinstance(dbsource_id, (int, long)) and dbsource_id[0] or dbsource_id
                    vals = {'event_id': event_id,
                            'responsible_id': responsible_id,
                            'invoice_partner_id': invoice_partner_id,
                            'name': responsible_brw.name,
                            'phone': responsible_brw.phone or responsible_brw.mobile or partner_brw.phone or partner_brw.mobile or None,
                            'email': responsible_brw.email or partner_brw.email or None,
                            'imported_dbsource_id': imported_dbsource_id,
                            'imported_id': rowID,
                            }
                    if already_imported and update:
                        registration_id = self.pool.get("event.registration").write(cr, uid, already_imported, vals, context)
                    else:
                        registration_id = self.pool.get("event.registration").create(cr, uid, vals, context)
                        vals = {'registration_id': registration_id,
                                'partner_id': partner_id,
                                'name': partner_brw.name,
                                'phone': partner_brw.phone or partner_brw.mobile or None,
                                'email': partner_brw.email or None,
                                'registration_type_id': registration_type_id,
                                'access_group_id': access_group_id,
                                'price': price,
                                'badge_name': badge_name,
                                'badge_company_name': badge_company_name,
                                'state': 'draft',
                                }
                        registration_line_id = self.pool.get("event.registration.line").create(cr, uid, vals, context)
        return True

    def searchAndCreateIfNeeded(self, cr, uid, registration, type='participante', partner_id=None, responsible_id=None, update=False, dbsource_id=None, rowID=None, context={}):
        if dbsource_id and not isinstance(dbsource_id, (int, long)):
            dbsource_id = dbsource_id[0]
        if type=="responsible": # Do we need to create the responsible_id???
            if 'participante_responsavel_check' in registration and registration['participante_responsavel_check']=='sim':
                return partner_id
            else:
                type = 'responsible'
        elif type=="invoice": # Do we need to create the invoice_partner_id???
            if 'nota_participante_check' in registration and registration['nota_participante_check']=='sim':
                return partner_id
            else:
                if 'nota_responsavel_check' in registration and registration['nota_responsavel_check']=='sim':
                    return responsible_id
                else:
                    type = "invoice"

        fieldMap = {'partner': {    'name': 'nome_text',
                                    'cnpj_cpf': 'cpf_text',
                                    'cnpj_cpf_invalido': 'cpf_text',
                                    'is_company': 'cpf_cnpj_radio',
                                    'l10n_br_city_id': 'cidade_text',
                                    'state_id': 'estado_select',
                                    'country_id': 'pais_select',
                                    'email': 'email_text',
                                    'phone': 'telefone_text',
                                    'mobile': 'fax_text',
                                    'function': 'cargo_text',
                                    'area': 'area_text',
                                    'street': 'endereco_text',
                                    'district': 'bairro_text',
                                    'zip': 'cep_text',
                                    },
                    'responsible': {'name': 'nome_completo_resp_text',
                                    'cnpj_cpf': 'cpf_responsavel_text',
                                    'cnpj_cpf_invalido': 'cpf_responsavel_text',
                                    'is_company': 'cpf_responsavel_text',
                                    'l10n_br_city_id': 'cidade_resp_text',
                                    'state_id': 'estado_resp_select',
                                    'country_id': 'pais_responsabel_select',
                                    'email': 'email_resp_text',
                                    'phone': 'telefone_resp_text',
                                    'mobile': 'fax_resp_text',
                                    'function': 'cargo_resp_text',
                                    'area': 'area_resp_text',
                                    'street': 'endereco_resp_text',
                                    'district': 'bairro_responsavel_text',
                                    'zip': 'cep_resp_text',
                                    },
                    'invoice': {    'name': 'nome_razao_social_text',
                                    'cnpj_cpf': 'cpf_nota_fiscal_text',
                                    'cnpj_cpf_invalido': 'cpf_nota_fiscal_text',
                                    'is_company': 'cpf_cnpj_nota_fiscal_radio',
                                    'l10n_br_city_id': 'cidade_nota_fiscal_text',
                                    'state_id': 'estado_nota_fiscal_text',
                                    'country_id': 'pais_nota_fiscal_select',
                                    'email': 'email_resp_text',
                                    'street': 'endereco_nota_fiscal_text',
                                    'district': 'bairro_nota_fiscal_text',
                                    'zip': 'cep_nota_fiscal_text',
                                    },
                    }
        if not type in fieldMap:
            raise osv.except_osv(_('Error!'),_("Specified importation record TYPE %s is not correct") % (type, ))
        fieldMap = fieldMap[type]
        # Primero buscamos al participante
        if not fieldMap['name'] in registration or not registration[fieldMap['name']]:
                _logger.info("Estamos pasando del registro porque no tiene %s" % (fieldMap['name']))
                return None
        if not fieldMap['cnpj_cpf'] in registration or not registration[fieldMap['cnpj_cpf']]:
                _logger.info("Estamos pasando del registro porque no tiene %s" % (fieldMap['cnpj_cpf']))
                return None
        partner_id = None
        # Creamos el partner
        to_review = False
        # Comprobamos que el cpf sea válido
        if fieldMap['is_company'] in registration and registration[fieldMap['is_company']]=='02': # is_company=true:
            if not fiscal.validate_cnpj(registration[fieldMap['cnpj_cpf']]):
                to_review = True
                cnpj_cpf = None
                cnpj_cpf_invalido = registration[fieldMap['cnpj_cpf']]
            else:
                cnpj_cpf = registration[fieldMap['cnpj_cpf']]
                cnpj_cpf_invalido = registration[fieldMap['cnpj_cpf']]
        else:
            if not fiscal.validate_cpf(registration[fieldMap['cnpj_cpf']]):
                to_review = True
                cnpj_cpf = None
                cnpj_cpf_invalido = registration[fieldMap['cnpj_cpf']]
            else:
                cnpj_cpf = registration[fieldMap['cnpj_cpf']]
                cnpj_cpf_invalido = registration[fieldMap['cnpj_cpf']]
        # Country
        country_id = None
        if fieldMap['country_id'] in registration and registration[fieldMap['country_id']] or False:
            country_id = self.pool.get("res.country").search(cr, uid, [('code', '=', registration[fieldMap['country_id']])])
            if country_id and not isinstance(country_id, (int, long)):
                country_id = country_id[0]
        # State
        state_id = None
        if fieldMap['state_id'] in registration and registration[fieldMap['state_id']] or False:
            state_id = self.pool.get("res.country.state").search(cr, uid, [('code', '=', registration[fieldMap['state_id']])])
            if state_id and not isinstance(state_id, (int, long)):
                state_id = state_id[0]
        # Country from State
        if not country_id and state_id:
            country_id = self.pool.get("res.country.state").browse(cr, uid, state_id, context).country_id.id
        # City
        city_id = None
        if fieldMap['l10n_br_city_id'] in registration and registration[fieldMap['l10n_br_city_id']] or False:
            city_id = self.pool.get("l10n_br_base.city").search(cr, uid, [('name', '=', registration[fieldMap['l10n_br_city_id']])])
            if city_id and not isinstance(city_id, (int, long)):
                city_id = city_id[0]
            # Comprobamos que la city tenga ibge_code
            if city_id:
                city_brw = self.pool.get("l10n_br_base.city").browse(cr, uid, city_id, context)
                if not city_brw.ibge_code:
                    to_review = True
            if not city_id and state_id: # Creamos la city si no existe
                to_review = True
                vals = {'name': registration[fieldMap['l10n_br_city_id']],
                        'state_id': state_id,
                        }
                city_id = self.pool.get("l10n_br_base.city").create(cr, uid, vals, context)
        # Preparamos el dict con los valores del partner
        vals = {'cnpj_cpf': cnpj_cpf,
                'cnpj_cpf_invalido': cnpj_cpf_invalido,
                'name': fieldMap['name'] in registration and registration[fieldMap['name']] or None,

                'email': fieldMap['email'] in registration and registration[fieldMap['email']] or None,
                'phone': 'phone' in fieldMap and fieldMap['phone'] in registration and registration[fieldMap['phone']] or None,
                'mobile': 'mobile' in fieldMap and fieldMap['mobile'] in registration and registration[fieldMap['mobile']] or None,
                'function': 'function' in fieldMap and fieldMap['function'] in registration and registration[fieldMap['function']] or None,
                'area': 'area' in fieldMap and fieldMap['area'] in registration and registration[fieldMap['area']] or None,
                'street': fieldMap['street'] in registration and registration[fieldMap['street']] or None,
                'district': fieldMap['district'] in registration and registration[fieldMap['district']] or None,
                'zip': fieldMap['zip'] in registration and registration[fieldMap['zip']] or None,
                'l10n_br_city_id': city_id,
                'state_id': state_id,
                'country_id': country_id,
                'to_review': to_review,
                'is_company': fieldMap['is_company'] in registration and registration[fieldMap['is_company']]=='02' or False,
                'imported_dbsource_id': dbsource_id,
                'imported_reg_id': rowID,
                }
        partner_id = self.pool.get("res.partner").search(cr, uid, ['|', ('cnpj_cpf', '=', registration[fieldMap['cnpj_cpf']]), ('cnpj_cpf_invalido', '=', registration[fieldMap['cnpj_cpf']])] )
        if partner_id:
            if not isinstance(partner_id, (int, long)):
                partner_id = partner_id[0]
            old_to_review = self.pool.get("res.partner").browse(cr, uid, partner_id, context).to_review
            vals.update({'to_review': to_review or old_to_review})
            if update:
                self.pool.get("res.partner").write(cr, uid, [partner_id], vals, context)
        else:
            partner_id = self.pool.get("res.partner").create(cr, uid, vals, context)
        return partner_id

    def _has_image(self, cr, uid, ids, name, args, context=None):
        result = {}
        for obj in self.browse(cr, uid, ids, context=context):
            result[obj.id] = obj.image != False
        return result

    def _num_participants(self, cr, uid, ids, name, args, context=None):
        result = {}
        for obj in self.browse(cr, uid, ids, context=context):
            participant_ids = self.pool.get('event.attendance').search(cr, uid, [('subevent_id', '=', obj.id)])
            # Clean duplicates
            registration_line_ids = {}
            for a in self.pool.get('event.attendance').browse(cr, uid, participant_ids, context):
                if not a.registration_line_id.id in registration_line_ids:
                    if a.action in ('check_in', 'forced_check_in'):
                        registration_line_ids[a.registration_line_id.id] = True
            result[obj.id] = len(registration_line_ids)
        return result

    _columns = {
        'image': fields.binary("Image", ),
        'has_image': fields.function(_has_image, type="boolean"),
        'parent_id': fields.many2one('event.event', 'Parent Event', ondelete="cascade"),
        'child_ids': fields.one2many('event.event', 'parent_id', 'Subevents', ),
        'speaker_ids': fields.many2many('event.speaker', 'event_event_event_speaker_rel', 'event_id', 'speaker_id', 'Other Speakers', ),
        'room_id': fields.many2one('event.room', 'Room'),
        'product_ids': fields.many2many('product.product', 'event_registration_product_product_rel', 'registration_id', 'product_id', 'Products', 
            domain=['&', ('sale_ok', '=', True), ('type', '=', 'service')],
            context={'default_sale_ok': 1, 'default_type': 'service'},
            ),
        'pricelist_id': fields.many2one('product.pricelist', 'Pricelist'),
        'registration_type_ids': fields.one2many('event.registration.type', 'event_id', 'Registration Types', ),
        'access_group_ids': fields.one2many('event.attendance.access.group', 'event_id', 'Access Groups', ),
        'state': fields.selection([
            ('draft', 'Unconfirmed'),
            ('confirm', 'Confirmed'),
            ('open', 'In Progress'),
            ('done', 'Done'),
            ('cancel', 'Cancelled')],
            'Status', readonly=True, required=True,
            track_visibility='onchange',
            help='If event is created, the status is \'Draft\'.If event is confirmed for the particular dates the status is set to \'Confirmed\'. If the vent is running, the status is In Progress. If the event is over, the status is set to \'Done\'.If event is cancelled the status is set to \'Cancelled\'.'),
        'ean_sequence_id': fields.many2one('ir.sequence', 'Barcode Sequence'),
        'num_participants': fields.function(_num_participants, type="integer", string="# Attendees"),
    }

    ######################
    # Workflow functions #
    ######################

    def action_draft(self, cr, uid, ids, context=None):
        """
        Set event as draft
        If reactivating, unconfirm all registrations
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        # TODO: Reactivating what?
        return self.write(cr, uid, ids, {'state': 'draft'}, context=context)

    def action_confirm(self, cr, uid, ids, context=None):
        """
        Set event as confirmed
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'confirm'}, context=context)

    def action_open(self, cr, uid, ids, context=None):
        """
        Set event as open
        """
        return self.write(cr, uid, ids, {'state': 'open'}, context=context)

    def action_done(self, cr, uid, ids, context=None):
        """
        Set event as done
        """
        return self.write(cr, uid, ids, {'state': 'done'}, context=context)

    def action_cancel(self, cr, uid, ids, context=None):
        """
        Set event as cancelled
        """
        return self.write(cr, uid, ids, {'state': 'cancel'}, context=context)

    def confirm_registrations(self, cr, uid, ids, context=None):
        """
        Confirm all registrations
        """
        # TODO: Test if registrations present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for event in self.browse(cr, uid, ids, context):
            for registration_id in event.registration_ids:
                wf_service.trg_validate(uid, 'event.registration', registration_id.id, 'confirm', cr)
        return True

    def open_registration_lines(self, cr, uid, ids, context=None):
        """
        Open all registrations lines
        """
        # TODO: Test if registrations present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for event in self.browse(cr, uid, ids, context):
            for registration_brw in event.registration_ids:
                for registration_line_brw in registration_brw.registration_line_ids:
                    wf_service.trg_validate(uid, 'event.registration.line', registration_line_brw.id, 'open', cr)
        return True

    def close_registrations(self, cr, uid, ids, context=None):
        """
        Close all registrations
        """
        # TODO: Test if registrations present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for event in self.browse(cr, uid, ids, context):
            for registration_brw in event.registration_ids:
                wf_service.trg_validate(uid, 'event.registration', registration_brw.id, 'close', cr)
        return True

    def cancel_registrations(self, cr, uid, ids, context=None):
        """
        Cancel all registrations
        """
        # TODO: Test if registrations present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for event in self.browse(cr, uid, ids, context):
            for registration_brw in event.registration_ids:
                wf_service.trg_validate(uid, 'event.registration', registration_brw.id, 'cancel', cr)
        return True

    def check_min_registrations(self, cr, uid, ids, *args):
        if isinstance(ids, (int, long)):
            ids = [ids]
        ok = True
        # TODO: Test if registrations present
        for event in self.browse(cr, uid, ids):
            total_confirmed = event.register_current
            if total_confirmed < event.register_min or total_confirmed > event.register_max and event.register_max!=0:
                raise osv.except_osv(_('Error!'),_("The total of confirmed registration for the event '%s' does not meet the expected minimum/maximum. Please reconsider those limits before going further.") % (event.name))
                ok = ok and False
        return ok

    def check_dates(self, cr, uid, ids, *args):
        today = fields.datetime.now()
        for event in self.browse(cr, uid, ids):
            if today < event.date_begin:
                raise osv.except_osv(_('Error!'), _("You must wait for the starting day of the event to do this action."))
        return True

class event_registration(osv.osv):
    _name= 'event.registration'
    _inherit= ['event.registration', 'mail.thread', 'ir.needaction_mixin']

    def _url_pagseguro(self, cr, uid, ids, name, args, context=None):
        result = {}
        for obj in self.browse(cr, uid, ids, context=context):
            result[obj.id] = ""
            #if obj.state in ('pending_payment') and obj.total_price > 0.0:
            #    url = ""
            #    resultado = cStringIO.StringIO()
            #    name = obj.invoice_partner_id and obj.invoice_partner_id.name or obj.responsible_id and obj.responsible_id.name or obj.name or obj.registration_line_ids and ("%s (%s)" % (obj.registration_line_ids[0].badge_name, obj.registration_line_ids[0].badge_company_name)) or 'Attendee'
            #    if name:
            #        name = re.sub(r'[^a-zA-Z0-9 ]', '', name)
            #    if name: # Quita espacios
            #        name = name.strip()
            #    if not ' ' in name:
            #        name = ''
            #    cep = obj.invoice_partner_id and obj.invoice_partner_id.zip or obj.responsible_id and obj.responsible_id.zip or ''
            #    if cep:
            #        cep = re.sub(r'[^0-9]', '', cep)
            #        if len(cep)!=8:
            #            cep = ''
            #    cnpj_cpf = obj.invoice_partner_id and obj.invoice_partner_id.cnpj_cpf or obj.responsible_id and obj.responsible_id.cnpj_cpf or obj.registration_line_ids and obj.registration_line_ids[0].partner_id and obj.registration_line_ids[0].partner_id.cnpj_cpf or ''
            #    if cnpj_cpf:
            #        cnpj_cpf = re.sub(r'[^0-9]', '', cnpj_cpf)
            #        if not fiscal.validate_cpf(cnpj_cpf):
            #            cnpj_cpf = ''
            #    data = {'email': 'administracao@advbpe.org.br',
            #            'token': '1A13722E1F8041318292A5681F70C00D',
            #            'currency': 'BRL',
            #            'itemId1': str(obj.id),
            #            'itemDescription1': 'Registration SCB 2013',
            #            'itemAmount1': "%.2f" % (obj.total_price,),
            #            'itemQuantity1': '1',
            #            'reference': str(obj.id),
            #            'shippingType': '3',
            #            }
            #    if name:
            #        data.update({'senderName': name.encode('utf8'),})
            #    if cep:
            #        data.update({'shippingAddressPostalCode': cep.encode('utf8'),})
            #    if cnpj_cpf:
            #        data.update({'senderCPF': cnpj_cpf.encode('utf8'),})
            #    data = urllib.urlencode(data)
            #    curl = pycurl.Curl()
            #    curl.setopt(curl.URL, "https://ws.pagseguro.uol.com.br/v2/checkout/")
            #    curl.setopt(curl.POST, 1)
            #    curl.setopt(curl.POSTFIELDS, data)
            #    curl.setopt(curl.WRITEFUNCTION, resultado.write)
            #    try:
            #        curl.perform()
            #    except Exception, e:
            #        _logger.error("e = %s" % (e, ))
            #    curl.close()
            #    response = resultado.getvalue()
            #    doc = etree.XML(response)
            #    code = ''
            #    nodes = doc.xpath("//code")
            #    for node in nodes:
            #        code = node.text
            #    url = ""
            #    if len(code)==32:
            #        url = "https://pagseguro.uol.com.br/v2/checkout/payment.html?code=%s" % code
            #else:
            #    url = ""
            #result[obj.id] = url
        return result

    def name_get(self, cr, uid, ids, context=None):
        if not len(ids):
            return []
        if context is None:
            context = {}
        res = []
        for r in self.pool.get('event.registration').browse(cr, uid, ids, context=context):
            #if r.name:
            #    res.append((r.id, "%s (%d)" % (r.name, r.nb_register)))
            #elif r.responsible_id:
            if r.responsible_id:
                res.append((r.id, "%s (%d)" % (r.responsible_id.name, r.nb_register)))
            elif r.invoice_partner_id:
                res.append((r.id, "%s (%d)" % (r.invoice_partner_id.name, r.nb_register)))
            elif len(r.registration_line_ids)==1:
                res.append((r.id, "%s (%d)" % (r.registration_line_ids[0].name, r.nb_register)))
            elif len(r.registration_line_ids)>1:
                res.append((r.id, "%s (%d)" % (", ".join([a.name for a in r.registration_line_ids]), r.nb_register)))
            else:
                res.append((r.id, "Empty registration"))
        return res

    def button_dummy(self, cr, uid, ids, context=None):
        return True

    def event_id_change(self, cr, uid, ids, event_id, context=None):
        domain = {}
        value = {}
        if not event_id:
            return {'domain':{'product_id':[]}}
        event_brw = self.pool.get("event.event").browse(cr, uid, event_id, context)
        product_ids = [product.id for product in event_brw.product_ids]
        if product_ids:
            domain = {'product_id':[('id', 'in', product_ids)]}
        if event_brw.pricelist_id:
            value.update({'pricelist_id': event_brw.pricelist_id.id})
        if event_brw.registration_type_ids:
            value.update({'type_ids': [a.id for a in event_brw.registration_type_ids]})
        return {    'value': value,
                    'domain': domain,
                    }

    def onchange_partner_id(self, cr, uid, ids, part, responsible_id, invoice_partner_id, context=None):
        res_obj = self.pool.get('res.partner')
        data = {}
        if not part:
            return {'value': data}
        addr = res_obj.address_get(cr, uid, [part]).get('default', False)
        if not responsible_id:
            addr_resp = res_obj.address_get(cr, uid, [part]).get('default', addr)
            data.update({'responsible_id': addr_resp})
        if not invoice_partner_id:
            addr_inv = res_obj.address_get(cr, uid, [part]).get('invoice', addr)
            data.update({'invoice_partner_id': addr_inv})
        if addr:
            d = self.onchange_contact_id(cr, uid, ids, addr, part, context)
            data.update(d['value'])
        return {'value': data}

    def onchange_responsible_id(self, cr, uid, ids, responsible_id, invoice_partner_id, context=None):
        res_obj = self.pool.get('res.partner')
        data = {}
        if not responsible_id:
            return {'value': data}
        addr = res_obj.address_get(cr, uid, [responsible_id]).get('default', False)
        if not invoice_partner_id:
            addr_resp = res_obj.address_get(cr, uid, [responsible_id]).get('default', addr)
            data.update({'invoice_partner_id': addr_resp})
        if addr:
            d = self.onchange_contact_id(cr, uid, ids, addr, responsible_id, context)
            data.update(d['value'])
        return {'value': data}

    def onchange_contact_id(self, cr, uid, ids, contact, partner, context=None):
        if not contact:
            return {}
        addr_obj = self.pool.get('res.partner')
        contact_id =  addr_obj.browse(cr, uid, contact, context=context)
        return {'value': {
            'email':contact_id.email,
            'name':contact_id.name,
            'phone':contact_id.phone or contact_id.mobile,
            }}

    def _total_price(self, cr, uid, ids, prop, unknow_none, unknow_dict):
        res = {}
        for registration_brw in self.browse(cr, uid, ids):
            totalPrice = 0.0
            if registration_brw.registration_line_ids:
                for line in registration_brw.registration_line_ids:
                    totalPrice += line.price
                res[registration_brw.id] = totalPrice
            else:
                res[registration_brw.id] = 0.0
        return res

    def _attendees_name_list(self, cr, uid, ids, prop, unknow_none, unknow_dict):
        res = {}
        for registration_brw in self.browse(cr, uid, ids):
            name_list = ""
            if registration_brw.registration_line_ids:
                name_list = ", ".join([a.badge_name for a in registration_brw.registration_line_ids if a.badge_name])
            res[registration_brw.id] = name_list
        return res

    def _attendees_ean13_list(self, cr, uid, ids, prop, unknow_none, unknow_dict):
        res = {}
        for registration_brw in self.browse(cr, uid, ids):
            ean13_list = ""
            if registration_brw.registration_line_ids:
                ean13_list = ", ".join([a.ean13 for a in registration_brw.registration_line_ids])
            res[registration_brw.id] = ean13_list
        return res

    def _type_ids(self, cr, uid, ids, prop, unknow_none, unknow_dict):
        res = {}
        for registration_brw in self.browse(cr, uid, ids):
            type_list = []
            if registration_brw.event_id.registration_type_ids:
                type_list = [a.id for a in registration_brw.event_id.registration_type_ids]
            res[registration_brw.id] = type_list
        return res

    def _nb_register(self, cr, uid, ids, prop, unknow_none, unknow_dict):
        res = {}
        for registration_brw in self.browse(cr, uid, ids):
            if registration_brw.registration_line_ids:
                res[registration_brw.id] = len(registration_brw.registration_line_ids)
            else:
                res[registration_brw.id] = 0
        return res

    def _get_registration(self, cr, uid, ids, context=None):
        result = {}
        for line in self.pool.get('event.registration.line').browse(cr, uid, ids, context=context):
            result[line.registration_id.id] = True
        return result.keys()

    _columns = {
        'partner_id': fields.many2one('res.partner', 'Attendee', ondelete='cascade', states={'done': [('readonly', True)]}),
        # TODO: do we need this anymore?
        'partner_ids': fields.many2many('res.partner', 'event_registration_res_partner', 'registration_id', 'partner_id', 'Attendees', ),
        'responsible_id': fields.many2one('res.partner', 'Responsible partner', ondelete='cascade'),
        'invoice_partner_id': fields.many2one('res.partner', 'Invoice Partner', ondelete='cascade'),
        'total_price': fields.function(_total_price, digits_compute=dp.get_precision('Product Price'), string='Total',
            store={
                'event.registration': (lambda self, cr, uid, ids, c={}: ids, ['registration_line_ids'], 10),
                'event.registration.line': (_get_registration, ['price'], 10),
            },
            help="The total of the registration"),
        'imported_dbsource_id': fields.many2one('base.external.dbsource', 'Importation - DB Source'),
        'imported_id': fields.integer('Importation - ID', ),
        'registration_line_ids': fields.one2many('event.registration.line', 'registration_id', 'Registration Lines', ),
        'attendees_name_list': fields.function(_attendees_name_list, type="char", size=1024, string="Attendees List",
            store=True),
            #store={
            #    'event.registration': (lambda self, cr, uid, ids, c={}: ids, ['registration_line_ids'], 10),
            #},
            #help="List of Attendees' names"),
        'attendees_ean13_list': fields.function(_attendees_ean13_list, type="char", size=1024, string="Attendees List",
            store={
                'event.registration': (lambda self, cr, uid, ids, c={}: ids, ['registration_line_ids'], 10),
            },
            help="List of Attendees' names"),
        'nb_register': fields.function(_nb_register, readonly=True, digits=(4,0), string='# Attendees',
            store={
                'event.registration': (lambda self, cr, uid, ids, c={}: ids, ['registration_line_ids'], 10),
            },
            help="The total number of participants in this registration."),
        'type_ids': fields.function(_type_ids, readonly=True, type="list", string='Registration Types', store=False),
        'state': fields.selection([ ('draft', 'Unverified'),
                                    ('verified', 'Verified'),
                                    ('pending_payment', 'Pending Payment'),
                                    ('paid', 'Paid'),
                                    ('open', 'Confirmed'),
                                    ('done', 'Attended'),
                                    ('cancel', 'Cancelled')], 'Status',
                                    track_visibility='onchange',
                                    size=16, readonly=True),
        'import_raw_data': fields.text("Dados originais da inscrição"),
        'pagseguro': fields.function(_url_pagseguro, type="char", size=256, string="PagSeguro", 
            store=False),
            #store={
            #    'event.registration': (lambda self, cr, uid, ids, c={}: ids, ['invoice_partner_id', 'total_price', 'registration_line_ids', 'state'], 10),
            #    'event.registration.line': (_get_registration, ['price', 'badge_name', 'badge_company_name', 'name', 'email', 'phone'], 10),
            #}, help="URL for PagSeguro",
            #),
    }

    _order = 'create_date DESC'

    ######################
    # Workflow functions #
    ######################

    def action_draft(self, cr, uid, ids, context=None):
        """
        Set Registration as draft
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'draft'}, context=context)

    def action_verify(self, cr, uid, ids, context=None):
        """
        Set Registration as verified
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'verified'}, context=context)

    def action_pending_payment(self, cr, uid, ids, context=None):
        """
        Set Registration as pending_payment
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'pending_payment'}, context=context)

    def action_paid(self, cr, uid, ids, context=None):
        """
        Set Registration as paid
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'paid'}, context=context)

    def action_refund(self, cr, uid, ids, context=None):
        """
        Do payment refund invoice
        """
        # TODO: Do payment refund invoice
        return True

    def action_confirm(self, cr, uid, ids, context=None):
        """
        Set Registration as open
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'open'}, context=context)

    def action_cancel(self, cr, uid, ids, context=None):
        """
        Set Registration as cancel
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'cancel'}, context=context)

    def confirm_lines(self, cr, uid, ids, context=None):
        """
        Confirm all registrations
        """
        # TODO: Test if registrations.lines present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for registration_brw in self.browse(cr, uid, ids, context):
            for registration_line_brw in registration_brw.registration_line_ids:
                wf_service.trg_validate(uid, 'event.registration.line', registration_line_brw.id, 'confirm', cr)
        return True

    def reset_lines(self, cr, uid, ids, context=None):
        """
        Reset all registrations
        """
        # TODO: Test if registrations.lines present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for registration_brw in self.browse(cr, uid, ids, context):
            for registration_line_brw in registration_brw.registration_line_ids:
                wf_service.trg_validate(uid, 'event.registration.line', registration_line_brw.id, 'reset', cr)
        return True

    def verify_lines(self, cr, uid, ids, context=None):
        """
        Verify all registrations lines
        """
        # TODO: Test if registrations.lines present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for registration_brw in self.browse(cr, uid, ids, context):
            for registration_line_brw in registration_brw.registration_line_ids:
                wf_service.trg_validate(uid, 'event.registration.line', registration_line_brw.id, 'verify', cr)
        return True

    def check_all_line_verify(self, cr, uid, ids, *args):
        if isinstance(ids, (int, long)):
            ids = [ids]
        ok = True
        for registration in self.browse(cr, uid, ids):
            if not registration.registration_line_ids:
                continue
            for registration_line_brw in registration.registration_line_ids:
                    ok = ok and registration_line_brw.state == 'verified'
        return ok

    def cancel_lines(self, cr, uid, ids, context=None):
        """
        Cancel all registrations
        """
        # TODO: Test if registrations.lines present
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for registration_brw in self.browse(cr, uid, ids, context):
            for registration_line_brw in registration_brw.registration_line_ids:
                wf_service.trg_validate(uid, 'event.registration.line', registration_line_brw.id, 'cancel', cr)
        return True

    def check_free(self, cr, uid, ids, *args):
        if isinstance(ids, (int, long)):
            ids = [ids]
        ok = True
        for registration in self.browse(cr, uid, ids):
            if registration.total_price!=0:
                ok = ok and False
        return ok

    def check_event_confirm(self, cr, uid, ids, *args):
        if isinstance(ids, (int, long)):
            ids = [ids]
        ok = True
        for registration in self.browse(cr, uid, ids):
            if not registration.event_id:
                continue
            ok = ok and registration.event_id.state in ('confirm', 'open')
        return ok

    def check_paid(self, cr, uid, ids, *args):
        if isinstance(ids, (int, long)):
            ids = [ids]
        ok = True
        # TODO: Check invoice paid
        return ok


class event_registration_line(osv.osv):
    _name= 'event.registration.line'

    def name_get(self, cr, uid, ids, context=None):
        if not len(ids):
            return []
        if context is None:
            context = {}
        res = []
        for r in self.browse(cr, uid, ids, context=context):
            name = ""
            if r.badge_name:
                name = "%s%s" % (name, r.badge_name, )
            if r.badge_company_name:
                name = "%s (%s)" % (name, r.badge_company_name, )
            res.append((r.id, name))
        return res

    def write(self, cr, uid, ids, vals, context=None):
        if 'room_id' in vals:
            vals.update({'room_last_time': fields.datetime.now()})
        return super(event_registration_line, self).write(cr, uid, ids, vals, context=context)

    def registration_type_id_change(self, cr, uid, ids, registration_type_id, context=None):
        if not registration_type_id:
            return {}
        registration_type_brw = self.pool.get("event.registration.type").browse(cr, uid, registration_type_id, context)
        return {    'value': {  'product_id': registration_type_brw.product_id.id or None,
                                'price': registration_type_brw.price or 0.0,
                                'access_group_id': registration_type_brw.access_group_id.id or None,
                                },
                    }

    def partner_id_change(self, cr, uid, ids, partner_id, context=None):
        if not partner_id:
            return {}
        partner_brw = self.pool.get("res.partner").browse(cr, uid, partner_id, context)
        return {    'value': {  'name': partner_brw.name or None,
                                'badge_name': partner_brw.name or None,
                                'email': partner_brw.email or None,
                                'phone': partner_brw.phone or partner_brw.mobile or None,
                                },
                    }

    def _get_ean_next_code(self, cr, uid, line_id, context=None):
        if context is None: context = {}
        sequence_obj = self.pool.get('ir.sequence')
        ean = ''
        line_brw = line_id
        sequence_id =  line_brw.registration_id and line_brw.registration_id.event_id and line_brw.registration_id.event_id.ean_sequence_id and line_brw.registration_id.event_id.ean_sequence_id.id or None
        if sequence_id:
            ean = sequence_obj.next_by_id(cr, uid, sequence_id, context=context)
        if len(ean) > 12:
            raise orm.except_orm(_("Configuration Error!"),
                 _("Configuration Error, you should define the barcode sequence on the event, and set it to generate 12 numbers (normally 6 for the company and 6 incremental)"))
        else:
            ean = (len(ean[0:6]) == 6 and ean[0:6] or ean[0:6].ljust(6,'0')) + ean[6:].rjust(6,'0')
        return ean
    
    def _get_ean_key(self, code):
        sum = 0
        for i in range(12):
            if isodd(i):
                sum += 3 * int(code[i])
            else:
                sum += int(code[i])
        key = (10 - sum % 10) % 10
        return str(key)
    
    def _generate_ean13_value(self, cr, uid, line, context=None):
        ean13 = False
        if context is None: context = {}
        ean = self._get_ean_next_code(cr, uid, line, context=context)
        if len(ean) != 12:
            raise orm.except_orm(_("Configuration Error!"),
                 _("This sequence is different than 12 characters. This can't work."
                   "You will have to redefine the sequence or create a new one"))
        key = self._get_ean_key(ean)
        ean13 = ean + key
        return ean13

    def generate_ean13(self, cr, uid, ids, context=None):
        if context is None: context = {}
        line_ids = self.browse(cr, uid, ids, context=context)
        for line in line_ids:
            if line.ean13:
                continue
            ean13 = self._generate_ean13_value(cr, uid, line, context=context)
            self.write(cr, uid, [line.id], {
                        'ean13': ean13
                    }, context=context)
        return True
   
    def create(self, cr, uid, vals, context=None):
        if context is None: context = {}
        id = super(event_registration_line, self).create(cr, uid, vals, context=context)
        if not vals.get('ean13'):
            ean13 = self.generate_ean13(cr, uid, [id], context=context)
        return id
    
    def copy(self, cr, uid, id, default=None, context=None):
        if default is None: default = {}
        if context is None: context = {}
        default['ean13'] = False
        return super(event_registration_line, self).copy(cr, uid, id, default=default, context=context)

    def _ean13_image(self, cr, uid, ids, name, args, context=None):
        result = {}
        bar = EanBarCode()
        for obj in self.browse(cr, uid, ids, context=context):
            result[obj.id] = "data:image/png;base64,%s" % bar.getImage(obj.ean13,25,"png")
        return result

    def _get_default_access_group(self, cr, uid, context=None):
        res = None
        return res

    def _get_color(self, cr, uid, ids, name, args, context=None):
        result = {}
        for obj in self.browse(cr, uid, ids, context=context):
            result[obj.id] = obj.access_group_id and obj.access_group_id.color or ''
        return result

    def _get_color_hex(self, cr, uid, ids, name, args, context=None):
        result = {}
        for obj in self.browse(cr, uid, ids, context=context):
            result[obj.id] = obj.access_group_id and obj.access_group_id.color_hex or ''
        return result

    def _resolve_event_id_from_context(self, cr, uid, context=None):
        if context is None:
            context = {}
        if type(context.get('default_event_id')) in (int, long):
            return context['default_event_id']
        if isinstance(context.get('default_event_id'), basestring):
            event_name = context['default_event_id']
            event_name = re.sub(' \(.+$', '', event_name)
            event_ids = self.pool.get('event.event').name_search(cr, uid, name=event_name, context=context)
            if len(event_ids) == 1:
                return event_ids[0][0]
        return None

    def _read_group_access_group_id(self, cr, uid, ids, domain, read_group_order=None, access_rights_uid=None, context=None):
        pool = self.pool.get('event.attendance.access.group')
        order = pool._order
        access_rights_uid = access_rights_uid or uid
        if read_group_order == 'access_group_id desc':
            order = '%s desc' % order
        search_domain = []
        event_id = self._resolve_event_id_from_context(cr, uid, context=context)
        if event_id:
            search_domain += ['|', ('event_id', '=', event_id)]
        search_domain += [('id', 'in', ids)]
        access_group_ids = pool._search(cr, uid, search_domain, order=order, access_rights_uid=access_rights_uid, context=context)
        result = pool.name_get(cr, access_rights_uid, access_group_ids, context=context)
        # restore order of the search
        result.sort(lambda x,y: cmp(access_group_ids.index(x[0]), access_group_ids.index(y[0])))
        fold = {}
        for access_group_brw in pool.browse(cr, access_rights_uid, access_group_ids, context=context):
            fold[access_group_brw.id] = False
        return result, fold

    def _read_group_room_id(self, cr, uid, ids, domain, read_group_order=None, access_rights_uid=None, context=None):
        pool = self.pool.get('event.room')
        order = pool._order
        access_rights_uid = access_rights_uid or uid
        if read_group_order == 'room_id desc':
            order = '%s desc' % order
        search_domain = []
        event_id = self._resolve_event_id_from_context(cr, uid, context=context)
        if event_id: # Look for all rooms of that event
            event_brw = self.pool.get('event.event').browse(cr, uid, event_id, context)
            if event_brw.room_id:
                ids.append(event_brw.room_id.id)
            for child_event_brw in event_brw.child_ids:
                if child_event_brw.room_id:
                    ids.append(child_event_brw.room_id.id)
        search_domain += [('id', 'in', ids)]
        room_ids = pool._search(cr, uid, search_domain, order=order, access_rights_uid=access_rights_uid, context=context)
        result = pool.name_get(cr, access_rights_uid, room_ids, context=context)
        # restore order of the search
        result.sort(lambda x,y: cmp(room_ids.index(x[0]), room_ids.index(y[0])))
        fold = {}
        for room_brw in pool.browse(cr, access_rights_uid, room_ids, context=context):
            fold[room_brw.id] = False
        return result, fold

    def name_search(self, cr, uid, name, args=None, operator='ilike', context=None, limit=100):
        if not args:
            args = []
        ids = self.search(cr, uid, [('name', operator, name)]+ args, limit=limit, context=context)
        ids += self.search(cr, uid, [('badge_name', operator, name)]+ args, limit=limit, context=context)
        ids += self.search(cr, uid, [('badge_company_name', operator, name)]+ args, limit=limit, context=context)
        ids += self.search(cr, uid, [('function', operator, name)]+ args, limit=limit, context=context)
        ids += self.search(cr, uid, [('ean13', '=', name)]+ args, limit=limit, context=context)
        #ids += self.search(cr, uid, [('cnpj_cpf', operator, name)]+ args, limit=limit, context=context)
        matchCPF = re.match(r'[0-9]+', name, re.M|re.I)
        if matchCPF:
            query = "select id from event_registration_line where regexp_replace(cnpj_cpf, '[^0-9]', '', 'g') ilike concat('%%', regexp_replace('%s', '[^0-9]', '', 'g'), '%%') and state in ('draft', 'verified', 'open', 'absent', 'present')" % name
            cr.execute(query)
            ids += filter(None, map(lambda x:x[0], cr.fetchall()))
        ids = list(set(ids))
        return self.name_get(cr, uid, ids, context)

    _columns = {
        'id': fields.integer('ID'),
        'ean13': fields.char('EAN13', size=13),
        'ean13_image': fields.function(_ean13_image, type="text"),
        'event_id': fields.related('registration_id', 'event_id', type='many2one', relation="event.event", string='Evento',
            store={
                'event.registration.line': (lambda self, cr, uid, ids, c={}: ids, ['registration_id'], 10),
            },
            help="Evento"),
        'registration_id': fields.many2one('event.registration', 'Registration', ondelete="cascade", required=True, readonly=True, ),
        'partner_id': fields.many2one('res.partner', 'Attendee', ondelete="cascade"),
        'name': fields.char('Name', size=128, select=True),
        'email': fields.char('Email', size=64),
        'phone': fields.char('Phone', size=64),
        'registration_type_id': fields.many2one('event.registration.type', 'Registration Type', required=True),
        'access_group_id': fields.many2one('event.attendance.access.group', 'Access group'),
        'product_id': fields.many2one('product.product', 'Product'),
        'price': fields.float('Unit Price', digits_compute=dp.get_precision('Product Price'), ),
        'badge_name': fields.char('Badge Name', size=128, select=True),
        'badge_company_name': fields.char('Badge Company Name', size=128, select=True),
        'state': fields.selection([ ('draft', 'Unverified'),
                                    ('verified', 'Verified'),
                                    ('open', 'Confirmed'),
                                    ('absent', 'Absent'),
                                    ('present', 'Present'),
                                    ('done', 'Attended'),
                                    ('cancel', 'Cancelled')], 'Status',
                                    track_visibility='onchange',
                                    size=16, readonly=True),
        # Fields only for usability pourpose
        'color': fields.function(_get_color, type="text"),
        'color_hex': fields.function(_get_color_hex, type="text"),
        'function': fields.related('partner_id', 'function', type='char', string='Function'),
        'cnpj_cpf': fields.related('partner_id', 'cnpj_cpf', type='char', string='CPF/CNPJ',
            store={
                'event.registration.line': (lambda self, cr, uid, ids, c={}: ids, ['partner_id'], 10),
            },
            help="Evento"),
        'room_id': fields.many2one('event.room', 'Room', readonly=True, track_visibility='onchange'),
        'room_last_time': fields.datetime('Date/Time of access to last room', readonly=True, track_visibility='onchange'),
        'badge_print_time': fields.datetime('Date/Time of badge print', readonly=True, track_visibility='onchange'),
        'pagseguro': fields.related('registration_id', 'pagseguro', type='char', string='PagSeguro',
            store=False),
            #store={
            #    'event.registration.line': (lambda self, cr, uid, ids, c={}: ids, ['registration_id', 'name', 'email', 'phone', 'registration_type_id', 'product_id', 'price', 'badge_name', 'badge_company_name', 'state'], 10),
            #},
            #help="URL for PagSeguro"),
    }

    _order= 'badge_name ASC'

    _defaults = {
        'access_group_id': _get_default_access_group,
    }

    _group_by_full = {
        'access_group_id': _read_group_access_group_id,
        'room_id': _read_group_room_id,
    }

    def print_badge(self, cr, uid, ids, context=None):
        '''
        This function prints the badge and saves the print data
        '''
        assert len(ids) == 1, 'This option should only be used for a single id at a time'
        # Save print data
        now = fields.datetime.now()
        self.pool.get("event.registration.line").write(cr, uid, ids, {'badge_print_time': now}, context)
        datas = {
                 'model': 'event.registration.line',
                 'ids': ids,
                 'form': self.read(cr, uid, ids[0], context=context),
        }
        return {'type': 'ir.actions.report.xml', 'report_name': 'report_webkit_event_badge', 'datas': datas, 'nodestroy': True}

    ######################
    # Workflow functions #
    ######################

    def action_draft(self, cr, uid, ids, context=None):
        """
        Set Registration Line as draft
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'draft'}, context=context)

    def action_confirm(self, cr, uid, ids, context=None):
        """
        Set Registration Line as open
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'open'}, context=context)

    def action_verify(self, cr, uid, ids, context=None):
        """
        Set Registration Line as verified
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        return self.write(cr, uid, ids, {'state': 'verified'}, context=context)

    def action_verify_registration(self, cr, uid, ids, context=None):
        """
        Send verify signal to registration
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        wf_service = netsvc.LocalService("workflow")
        for registration_line_brw in self.browse(cr, uid, ids, context):
            wf_service.trg_validate(uid, 'event.registration', registration_line_brw.registration_id.id, 'verify_line', cr)
        return True

    def action_cancel(self, cr, uid, ids, context=None):
        """
        Set Registration Line as cancelled
        """
        return self.write(cr, uid, ids, {'state': 'cancel'}, context=context)

    def send_confirm_email(self, cr, uid, ids, context=None):
        """
        Send Registration email
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        for registration_line_brw in self.browse(cr, uid, ids, context):
            if registration_line_brw.registration_id.event_id.email_confirmation_id:
                if registration_line_brw.state in ('open'):
                    self.mail_user_confirm(cr, uid, registration_line_brw.id)
        return True

    def mail_user_confirm(self, cr, uid, ids, context=None):
        """
        Send email to user when the event is confirmed
        """
        if not ids:
            return True
        if isinstance(ids, (int, long)):
            ids = [ids]
        for registration_line in self.browse(cr, uid, ids, context=context):
            template_id = registration_line.registration_id.event_id.email_confirmation_id.id
            if template_id:
                mail_message = self.pool.get('email.template').send_mail(cr,uid,template_id,registration_line.id)
        return True

class event_registration_type(osv.osv):
    _name= 'event.registration.type'

    def onchange_event_id(self, cr, uid, ids, event_id, context=None):
        domain = []
        if not event_id:
            return {'domain': domain}
        event_brw = self.pool.get('event.event').browse(cr, uid, event_id, context)
        if event_brw and event_brw.child_ids:
            domain = [('id', 'in', [c.id for c in event_brw.child_ids])]
        return {'domain': domain}

    def product_id_change(self, cr, uid, ids, product_id, pricelist_id, context=None):
        if not product_id:
            return {}
        product_brw = self.pool.get("product.product").browse(cr, uid, product_id, context)
        if product_brw.list_price:
            if pricelist_id:
                price = self.pool.get("product.pricelist").price_get(cr, uid, [pricelist_id], product_id, 1, context=context)
                if price and pricelist_id in price:
                    return {    'value':{   'price': price[pricelist_id],
                                            }
                                }
            else:
                return {'value':{'price': product_brw.list_price}}
        return {}

    _columns = {
        'name': fields.char('Name', size=64, required=True),
        'sequence': fields.integer('Sequence', ),
        'event_id': fields.many2one('event.event', 'Event', required=True, ondelete="cascade", domain=[('parent_id', '=', False)]),
        'pricelist_id': fields.many2one('product.pricelist', 'Pricelist'),
        'product_id': fields.many2one('product.product', 'Related Product'),
        'price': fields.float('Unit Price', digits_compute=dp.get_precision('Product Price'), ),
        'access_group_id': fields.many2one('event.attendance.access.group', 'Default Access group'),
        #'event_ids': fields.many2many('event.event', 'event_event_event_registration_type_rel', 'event_id', 'registration_type_id', 'Access to events', domain=[('parent_id', '!=', False)]),
        #'color': fields.char('Badge Color', help='Color for Badge (Ex. #FFFF00', size=7, ),
        'form_class_value': fields.char('Form Class Value', size=64),
    }

class ir_sequence(orm.Model):
    _inherit = 'ir.sequence'
    
    _columns = {
        'barcode_sequence': fields.boolean('Barcode Sequence'),
    }
    
    _defaults = {
        'barcode_sequence': False,
    }
    
class event_attendance(osv.osv):
    _name= 'event.attendance'

    # TODO: def get_registration_line_by_ean13
    # TODO: def authorize(registration_line)

    _columns = {
        'name': fields.datetime('Date', required=True, select=1),
        'session_id': fields.many2one('event.attendance.wizard', 'Event Session'),
        'event_id': fields.many2one('event.event', 'Event'),
        'subevent_id': fields.many2one('event.event', 'Subevent'),
        'room_id': fields.many2one('event.room', 'Room'),
        'ean13': fields.char('EAN13', size=13, required=True),
        'registration_line_id': fields.many2one('event.registration.line', 'Registration Line'),
        'registration': fields.related('registration_line_id', 'registration_id', type='many2one', relation='event.registration', string='Registration'),
        'badge_name': fields.related('registration_line_id', 'badge_name', type='char', string='Badge Name'),
        'badge_company_name': fields.related('registration_line_id', 'badge_company_name', type='char', string='Badge Company Name'),
        'function': fields.char('Function', size=128),
        'action': fields.selection([    ('check_in', 'Check In'), 
                                        ('check_out', 'Check Out'), 
                                        ('forbidden', 'Forbidden'), 
                                        ('forced_check_in', 'Force Check In'), 
                                        ], 'Action', required=True),
        # Fields only for usability pourpose
        'partner_id': fields.related('registration_line_id', 'partner_id', type='many2one', relation='res.partner', string='Partner', store=True),
        'access_group_id': fields.related('registration_line_id', 'access_group_id', type='many2one', relation='event.attendance.access.group', string='Access Group', store=True),
        'cnpj_cpf': fields.related('registration_line_id', 'cnpj_cpf', type='char', string='CNPJ / CPF', store=True),
    }

    _defaults = {
        'name': lambda *a: time.strftime('%Y-%m-%d %H:%M:%S'),
    }

    _order = 'name desc'

class event_attendance_access_group(osv.osv):
    _name= 'event.attendance.access.group'

    def onchange_event_id(self, cr, uid, ids, event_id, context=None):
        domain = []
        if not event_id:
            return {'domain': domain}
        event_brw = self.pool.get('event.event').browse(cr, uid, event_id, context)
        if event_brw and event_brw.child_ids:
            domain = [('id', 'in', [c.id for c in event_brw.child_ids])]
        return {'domain': domain}

    _columns = {
        'name': fields.char('Name', size=64, required=True),
        'sequence': fields.integer('Sequence', ),
        'color': fields.char('Color', size=64, help="CSS Color for Badge. Ex: #FC34A2"),
        'event_id': fields.many2one('event.event', 'Event', required=True, ondelete="cascade", domain=[('parent_id', '=', False)]),
        'event_ids': fields.many2many('event.event', 'event_event_event_attendance_access_group_rel', 'event_id', 'access_group_id', 'Access to events', domain=[('parent_id', '!=', False)]),
        # Fields only for usability poupose
        'color_hex': fields.char('Color (HEX)', size=64, help="CSS Color for Badge. Ex: #FC34A2"),
    }

    _defaults = {
        'color': 'white',
        # TODO: Improme to add webcolors.name_to_hex and hex_to_name
        'color_hex': '#ffffff',
    }

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
