# -*- coding: utf-8 -*-
{
    "name" : "SOMA - ADVB PE",
    "version" : "0.1",
    "author" : "Grupo CITEC",
    "category": 'SOMA',
    'complexity': "easy",
    "description": """
Sistema de gerenciamento de Empresas SOMA
=========================================
Adaptação do sistema OpenERP para a empresa ADVB PE
Programado pelo Grupo CITEC Ltda.

v0.1
    """,
    'website': 'http://www.grupocitec.com',
    "depends" : [
    	"base", 
    	"event", 
    	"portal_event", 
    	"product", 
    	"report_webkit", 
	],
    'init_xml': [],
    'update_xml': [
        "view/not_ready_view.xml",
        "report/badge_webkit_report.xml",
        "report/registration_line_list.xml",
        "wizard/event_attendance_view.xml",
        "wizard/quick_registration_view.xml",
        "view/event_view.xml",
        "data/advbpe_cron.xml",
        "view/res_partner_view.xml",
        "workflow/event_workflow.xml",
        "view/sequence_view.xml",
        "view/base_external_dbsource_view.xml",
    ],
    'data': [
        "data/email_template.xml",
        "data/sequence.xml",
    ],
    'demo_xml': [],
    'test': [],
    'application': True,
    'installable': True,
    'css': [
        'static/src/css/event.css'
    ],
}
# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
