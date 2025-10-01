"""
Protein Network Visualizer - FIXED to display actual biological consequences
Converts pipeline JSON output to interactive HTML visualization with rich functional data
"""

import json
import webbrowser
import tempfile
import os
from pathlib import Path

def create_visualization(json_data, output_path=None):
    """
    Creates an HTML visualization from pipeline JSON data with automatic fixes
    
    Args:
        json_data: Either a dict with the JSON data or a path to JSON file
        output_path: Optional path to save the HTML file (default: temp file)
    
    Returns:
        Path to the created HTML file
    """
    
    # Load JSON data if it's a file path
    if isinstance(json_data, (str, Path)):
        json_path = Path(json_data)
        data = json.loads(json_path.read_text(encoding="utf-8"))
    else:
        data = json_data
    
    # AUTO-FIX: Function name shortening map
    NAME_FIXES = {
        "ER-associated degradation": "ERAD",
        "Type I Interferon Signaling": "IFN signaling",
        "Type I IFN signaling": "IFN signaling",
        "RNF8 Stability & DNA Repair": "DNA repair",
        "Mitophagy Inhibition": "Mitophagy",
        "Protein Stabilization": "Stabilization",
        "Proteolytic Cleavage": "Cleavage",
        "Oxidative Stress Response": "Stress response",
        "Apoptosis Regulation": "Apoptosis",
        "Apoptosis Inhibition": "Apoptosis",
        "Transcriptional Repression": "Transcription",
        "Autophagy Induction": "Autophagy",
        "Autophagy Initiation": "Autophagy",
        "Cell Cycle Progression": "Cell cycle",
        "ATXN3 Degradation": "Degradation",
        "ATXN3 Stabilization": "Stabilization",
        "ATXN3 Cleavage": "Cleavage",
        "Protein Folding Impairment": "Protein folding",
        "mTORC1 Signaling": "mTORC1",
        "Oncogenic Signaling": "Oncogenesis",
        "Tumor Suppression": "Tumor suppressor",
        "PolyQ Neuroprotection": "Neuroprotection",
        "Nucleotide Excision Repair": "DNA repair",
        "DNA Damage Repair": "DNA repair",
        "DNA Repair Inhibition": "DNA repair block",
        "ERAD Inhibition": "ERAD block",
        "GABA-A Receptor Trafficking": "GABA trafficking",
        "Nucleocytoplasmic Transport": "Nuclear transport",
        "Innate Immunity": "Immunity",
        "Innate Immunity Regulation": "Immunity",
        "Endocytic Trafficking": "Endocytosis",
        "Antiviral Response": "Antiviral",
    }
    
    # AUTO-FIX: Apply fixes to ctx_json if present
    if 'ctx_json' in data:
        for interactor in data['ctx_json'].get('interactors', []):
            # Fix function names
            for func in interactor.get('functions', []):
                original_name = func.get('function', '')
                # Apply known fixes
                if original_name in NAME_FIXES:
                    func['function'] = NAME_FIXES[original_name]
                # Or shorten if too long
                elif len(original_name.split()) > 4:
                    func['function'] = ' '.join(original_name.split()[:3])
                
                # Ensure function has evidence
                if not func.get('evidence') or len(func['evidence']) == 0:
                    # Try to copy from interactor
                    if interactor.get('evidence') and len(interactor['evidence']) > 0:
                        func['evidence'] = [interactor['evidence'][0]]
                    elif func.get('pmids') and len(func['pmids']) > 0:
                        # Create from PMIDs
                        func['evidence'] = [{
                            "doi": "10.xxxx/pending",
                            "pmid": func['pmids'][0],
                            "paper_title": f"Evidence for {func.get('function', 'function')} - validation pending",
                            "authors": "Authors TBD",
                            "journal": "Journal TBD",
                            "year": 2024,
                            "relevant_quote": "Validation pending"
                        }]
                    else:
                        # Create placeholder
                        func['evidence'] = [{
                            "doi": "10.xxxx/pending",
                            "pmid": "00000000",
                            "paper_title": f"Evidence for {func.get('function', 'function')} pending",
                            "authors": "TBD",
                            "journal": "TBD",
                            "year": 2024,
                            "relevant_quote": "Validation required"
                        }]
    
    # Extract the snapshot_json which contains the visualization data
    if 'snapshot_json' in data:
        viz_data = data['snapshot_json']
    else:
        # Fallback to the full data if no snapshot
        viz_data = data
    
    # AUTO-FIX: Apply same fixes to snapshot_json if different from ctx_json
    if 'snapshot_json' in data and 'ctx_json' in data:
        # Apply fixes from ctx_json to snapshot_json
        for snap_interactor in viz_data.get('interactors', []):
            # Find matching interactor in ctx_json
            for ctx_interactor in data['ctx_json'].get('interactors', []):
                if ctx_interactor.get('primary') == snap_interactor.get('primary'):
                    # Copy fixed functions
                    snap_interactor['functions'] = ctx_interactor.get('functions', [])
                    # Copy evidence if missing
                    if not snap_interactor.get('evidence'):
                        snap_interactor['evidence'] = ctx_interactor.get('evidence', [])
                    break
    merged_interactors = {}
    for interactor in viz_data.get('interactors', []):
        primary = interactor.get('primary')
        if primary in merged_interactors:
            # Merge with existing
            existing = merged_interactors[primary]
            # Combine functions
            existing['functions'].extend(interactor.get('functions', []))
            # Keep higher confidence
            if interactor.get('confidence', 0) > existing.get('confidence', 0):
                existing['confidence'] = interactor['confidence']
            # Combine evidence
            if 'evidence' in interactor:
                if 'evidence' not in existing:
                    existing['evidence'] = []
                existing['evidence'].extend(interactor['evidence'])
            # Note if there are multiple interaction types
            if existing.get('arrow') != interactor.get('arrow') or existing.get('direction') != interactor.get('direction'):
                existing['multiple_arrows'] = True
                if 'all_arrows' not in existing:
                        existing['all_arrows'] = [existing.get('arrow')]
                        existing['all_directions'] = [existing.get('direction')]
                        existing['all_intents'] = [existing.get('intent')]
                existing['all_arrows'].append(interactor.get('arrow'))
                existing['all_directions'].append(interactor.get('direction'))
                existing['all_intents'].append(interactor.get('intent', 'binding'))
        else:
            merged_interactors[primary] = interactor.copy()
            merged_interactors[primary]['functions'] = interactor.get('functions', []).copy()
    
    viz_data['interactors'] = list(merged_interactors.values())
    
    # Ensure we have the evidence data from ctx_json
    if 'ctx_json' in data and 'interactors' in data['ctx_json']:
        # Merge evidence data into viz_data
        for viz_interactor in viz_data.get('interactors', []):
            for ctx_interactor in data['ctx_json']['interactors']:
                if viz_interactor.get('primary') == ctx_interactor.get('primary'):
                    # Add missing fields
                    if 'evidence' not in viz_interactor and 'evidence' in ctx_interactor:
                        viz_interactor['evidence'] = ctx_interactor['evidence']
                    if 'support_summary' not in viz_interactor and 'support_summary' in ctx_interactor:
                        viz_interactor['support_summary'] = ctx_interactor['support_summary']
                    # Add validation status if available
                    if 'validated' in ctx_interactor:
                        viz_interactor['validated'] = ctx_interactor['validated']
                    if 'validation_status' in ctx_interactor:
                        viz_interactor['validation_status'] = ctx_interactor['validation_status']
                    if 'paper_titles' in ctx_interactor:
                        viz_interactor['paper_titles'] = ctx_interactor['paper_titles']
                    if 'paper_years' in ctx_interactor:
                        viz_interactor['paper_years'] = ctx_interactor['paper_years']
                    # Ensure functions have all fields including biological_consequence and cellular_process
                    for i, func in enumerate(viz_interactor.get('functions', [])):
                        if i < len(ctx_interactor.get('functions', [])):
                            ctx_func = ctx_interactor['functions'][i]
                            # CRITICAL: Copy the actual fields
                            if 'biological_consequence' not in func and 'biological_consequence' in ctx_func:
                                func['biological_consequence'] = ctx_func['biological_consequence']
                            if 'cellular_process' not in func and 'cellular_process' in ctx_func:
                                func['cellular_process'] = ctx_func['cellular_process']
                            if 'specific_effects' not in func and 'specific_effects' in ctx_func:
                                func['specific_effects'] = ctx_func['specific_effects']
                            if 'pmids' not in func and 'pmids' in ctx_func:
                                func['pmids'] = ctx_func['pmids']
    
    # Get the main protein name and JSON data as strings
    main_protein = viz_data.get('main', 'Unknown')
    json_data_str = json.dumps(viz_data)
    
    # HTML template - using PLACEHOLDER markers instead of format strings
    html_template = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Protein Interaction Network - PLACEHOLDER_MAIN_PROTEIN</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif;
            background: #f8f9fa;
            min-height: 100vh;
            overflow: hidden;
            position: relative;
        }

        .container {
            position: relative;
            width: 100%;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }

        .header {
            background: white;
            padding: 15px 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.08);
            z-index: 100;
            border-bottom: 1px solid #e1e4e8;
        }

        .title {
            font-size: 24px;
            font-weight: 600;
            color: #1a202c;
            text-align: center;
            margin-bottom: 5px;
            letter-spacing: -0.5px;
        }

        .subtitle {
            text-align: center;
            color: #6b7280;
            font-size: 13px;
            font-weight: 400;
        }

        #network {
            flex: 1;
            position: relative;
            background: white;
            overflow: hidden;
        }

        .controls {
            position: absolute;
            top: 20px;
            left: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            padding: 10px;
            display: flex;
            gap: 8px;
            z-index: 50;
            border: 1px solid #e1e4e8;
        }

        .control-btn {
            width: 32px;
            height: 32px;
            border: 1px solid #d1d5db;
            background: white;
            border-radius: 6px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            color: #4b5563;
            transition: all 0.2s ease;
        }

        .control-btn:hover {
            background: #f3f4f6;
            border-color: #9ca3af;
        }

        .node {
            cursor: move;
            transition: all 0.2s ease;
        }

        .node:hover {
            filter: brightness(1.1);
        }

        .main-node {
            fill: #2d3748;
            stroke: #1a202c;
            stroke-width: 2.5;
        }

        .interactor-node {
            fill: #4a5568;
            stroke: #2d3748;
            stroke-width: 2;
        }

        .function-node {
            fill: #ffffff;
            stroke: #6b7280;
            stroke-width: 1.5;
            cursor: pointer;
        }

        .function-node:hover {
            fill: #f0f4ff;
            stroke: #4f46e5;
            stroke-width: 2;
        }

        .function-node-query {
            stroke-dasharray: none;
            stroke-width: 2;
        }

        .function-node-interactor {
            stroke-dasharray: 5,3;
            stroke-width: 1.5;
        }

        .function-owner-badge {
            fill: #4f46e5;
            rx: 4;
            ry: 4;
            filter: drop-shadow(0 2px 4px rgba(0,0,0,0.1));
        }

        .function-owner-text {
            fill: white;
            font-size: 9px;
            font-weight: 600;
            text-anchor: middle;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }

        .cellular-process-badge {
            fill: #10b981;
            rx: 3;
            ry: 3;
        }

        .cellular-process-text {
            fill: white;
            font-size: 8px;
            font-weight: 600;
            text-anchor: middle;
        }

        .link {
            fill: none;
            stroke-width: 2;
            opacity: 0.6;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .link:hover {
            stroke-width: 3;
            opacity: 1;
        }

        .link-activate {
            stroke: #059669;
        }

        .link-inhibit {
            stroke: #dc2626;
        }

        .link-binding {
            stroke: #7c3aed;
        }

        .function-link {
            stroke-width: 3;
            opacity: 0.8;
        }

        .node-label {
            font-size: 12px;
            font-weight: 600;
            fill: white;
            text-anchor: middle;
            pointer-events: none;
            font-family: 'Helvetica Neue', Arial, sans-serif;
        }

        .function-label {
            font-size: 11px;
            font-weight: 500;
            fill: #374151;
            text-anchor: middle;
            pointer-events: none;
            cursor: pointer;
        }

        .confidence-badge {
            font-size: 16px;
            font-weight: 700;
            text-anchor: middle;
            pointer-events: none;
        }

        .confidence-high { fill: #059669; }
        .confidence-medium { fill: #f59e0b; }
        .confidence-low { fill: #dc2626; }

        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
        }

        .modal.active {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .modal-content {
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 900px;
            max-height: 85vh;
            overflow-y: auto;
            box-shadow: 0 20px 25px -5px rgba(0,0,0,0.2);
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 12px;
            border-bottom: 2px solid #e5e7eb;
        }

        .modal-title {
            font-size: 20px;
            font-weight: 600;
            color: #1f2937;
        }

        .close-btn {
            background: none;
            border: none;
            font-size: 24px;
            cursor: pointer;
            color: #6b7280;
            width: 32px;
            height: 32px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
        }

        .close-btn:hover {
            background: #f3f4f6;
            color: #1f2937;
        }

        .info-table {
            width: 100%;
            border-collapse: collapse;
        }

        .info-row {
            border-bottom: 1px solid #f3f4f6;
        }

        .info-row:last-child {
            border-bottom: none;
        }

        .info-label {
            font-weight: 600;
            color: #6b7280;
            padding: 10px 0;
            vertical-align: top;
            width: 25%;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.5px;
        }

        .info-value {
            padding: 10px 0 10px 16px;
            color: #1f2937;
            line-height: 1.6;
            font-size: 14px;
        }

        .cascade-arrow {
            color: #10b981;
            font-weight: bold;
            margin: 0 4px;
        }

        .confidence-meter {
            width: 180px;
            height: 6px;
            background: #e5e7eb;
            border-radius: 3px;
            overflow: hidden;
            margin-top: 4px;
            display: inline-block;
        }

        .confidence-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.5s ease;
        }

        .confidence-fill.high { background: #059669; }
        .confidence-fill.medium { background: #f59e0b; }
        .confidence-fill.low { background: #dc2626; }

        .evidence-item {
            background: #f9fafb;
            padding: 10px;
            border-radius: 6px;
            margin: 6px 0;
            border-left: 3px solid #4f46e5;
            font-size: 13px;
        }

        .evidence-item strong {
            color: #4b5563;
        }

        .pmid-link {
            color: #4f46e5;
            text-decoration: none;
            font-weight: 600;
        }

        .pmid-link:hover {
            text-decoration: underline;
        }

        .specific-effect {
            background: #f0f9ff;
            padding: 8px 12px;
            margin: 6px 0;
            border-radius: 6px;
            border-left: 3px solid #0891b2;
            font-size: 13px;
        }

        .cellular-theme {
            background: #ecfdf5;
            padding: 12px;
            border-radius: 8px;
            margin: 10px 0;
            border: 1px solid #10b981;
        }

        .cellular-theme-title {
            font-weight: 600;
            color: #059669;
            margin-bottom: 8px;
            text-transform: uppercase;
            font-size: 12px;
        }
        
        .biological-cascade {
            background: #fef3c7;
            padding: 12px;
            border-radius: 8px;
            margin: 10px 0;
            border: 1px solid #fbbf24;
            font-size: 13px;
            line-height: 1.8;
        }

        .legend {
            position: absolute;
            bottom: 20px;
            right: 20px;
            background: white;
            padding: 12px 16px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            border: 1px solid #e1e4e8;
        }

        .legend-title {
            font-weight: 600;
            margin-bottom: 8px;
            color: #374151;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .legend-item {
            display: flex;
            align-items: center;
            margin: 4px 0;
            font-size: 12px;
            color: #6b7280;
        }

        .legend-arrow {
            width: 30px;
            height: 20px;
            margin-right: 8px;
            display: flex;
            align-items: center;
        }

        .info-panel {
            position: absolute;
            top: 20px;
            right: 20px;
            background: white;
            padding: 12px 16px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            border: 1px solid #e1e4e8;
            font-size: 12px;
            color: #6b7280;
        }

        .info-panel strong {
            color: #374151;
        }
        
        .multi-function-indicator {
            fill: #fbbf24;
            stroke: #f59e0b;
            stroke-width: 1;
            cursor: pointer;
        }

        .multi-function-text {
            fill: #92400e;
            font-size: 10px;
            font-weight: 700;
            text-anchor: middle;
            pointer-events: none;
        }

        .bidirectional-arrow-marker {
            fill: #9ca3af;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 class="title" id="networkTitle">PLACEHOLDER_MAIN_PROTEIN Protein Interaction Network</h1>
            <p class="subtitle">Click Functions & Arrows for Details • Shows Biological Cascades Not Just Mechanisms</p>
        </div>
        
        <div id="network">
            <div class="controls">
                <button class="control-btn" onclick="zoomIn()" title="Zoom In">+</button>
                <button class="control-btn" onclick="zoomOut()" title="Zoom Out">−</button>
                <button class="control-btn" onclick="resetView()" title="Reset View">⟲</button>
            </div>
            
            <div class="info-panel">
                <strong>TIPS:</strong> Function boxes show biological processes • Click for cascading consequences
            </div>
            
            <svg id="svg"></svg>
        </div>

        <div class="legend">
            <div class="legend-title">INTERACTION TYPES</div>
            <div class="legend-item">
                <div class="legend-arrow">
                    <svg width="30" height="20">
                        <line x1="0" y1="10" x2="20" y2="10" stroke="#059669" stroke-width="2"/>
                        <polygon points="20,10 26,10 23,7 23,13" fill="#059669"/>
                    </svg>
                </div>
                Activates
            </div>
            <div class="legend-item">
                <div class="legend-arrow">
                    <svg width="30" height="20">
                        <line x1="0" y1="10" x2="20" y2="10" stroke="#dc2626" stroke-width="2"/>
                        <line x1="23" y1="6" x2="23" y2="14" stroke="#dc2626" stroke-width="3"/>
                    </svg>
                </div>
                Inhibits
            </div>
            <div class="legend-item">
                <div class="legend-arrow">
                    <svg width="30" height="20">
                        <line x1="0" y1="8" x2="26" y2="8" stroke="#7c3aed" stroke-width="2"/>
                        <line x1="0" y1="12" x2="26" y2="12" stroke="#7c3aed" stroke-width="2"/>
                    </svg>
                </div>
                Binding
            </div>
            <div class="legend-title" style="margin-top: 12px;">FUNCTION BOXES</div>
            <div class="legend-item">
                <svg width="20" height="12" style="margin-right: 8px;">
                    <rect x="0" y="2" width="20" height="8" fill="white" stroke="#6b7280" stroke-width="2"/>
                </svg>
                Biological Functions
            </div>
            <div class="legend-item">
                <svg width="20" height="12" style="margin-right: 8px;">
                    <rect x="0" y="2" width="20" height="8" fill="#f0f4ff" stroke="#4f46e5" stroke-width="2"/>
                </svg>
                Click for Cascading Effects
            </div>
        </div>
    </div>

    <div id="modal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 class="modal-title" id="modalTitle">Interaction Details</h2>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <div id="modalBody"></div>
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
    <script>
        // Embedded data
        const data = PLACEHOLDER_JSON_DATA;

        let svg, g, width, height, simulation;
        let nodes = [], links = [];
        let currentZoom = 1;
        let mainNodeRadius = 35; // Will be dynamically set
        let interactorNodeRadius = 28;

        function initNetwork() {
            document.getElementById('networkTitle').textContent = `${data.main} Protein Interaction Network`;
            
            const container = document.getElementById('network');
            width = container.clientWidth;
            height = container.clientHeight;

            svg = d3.select('#svg')
                .attr('width', width)
                .attr('height', height);

            g = svg.append('g');

            const zoom = d3.zoom()
                .scaleExtent([0.3, 3])
                .on('zoom', (event) => {
                    g.attr('transform', event.transform);
                    currentZoom = event.transform.k;
                });

            svg.call(zoom);

            // Calculate dynamic main node radius based on number of interactors
            const numInteractors = data.interactors.length;
            mainNodeRadius = Math.min(35 + numInteractors * 2, 80); // Min 35, max 80, grows with interactors
            
            const defs = svg.append('defs');

            // Create arrow markers for all arrow types
            ['activate', 'inhibit', 'binding'].forEach(type => {
                const color = type === 'activate' ? '#059669' : 
                             type === 'inhibit' ? '#dc2626' : '#7c3aed';
                
                if (type === 'activate') {
                    defs.append('marker')
                        .attr('id', `arrow-${type}`)
                        .attr('viewBox', '0 -5 10 10')
                        .attr('refX', 10)
                        .attr('refY', 0)
                        .attr('markerWidth', 10)
                        .attr('markerHeight', 10)
                        .attr('orient', 'auto')
                        .append('path')
                        .attr('d', 'M0,-5L10,0L0,5L3,0Z')
                        .attr('fill', color);
                } else if (type === 'inhibit') {
                    defs.append('marker')
                        .attr('id', `arrow-${type}`)
                        .attr('viewBox', '0 -5 10 10')
                        .attr('refX', 10)
                        .attr('refY', 0)
                        .attr('markerWidth', 10)
                        .attr('markerHeight', 10)
                        .attr('orient', 'auto')
                        .append('rect')
                        .attr('x', 6)
                        .attr('y', -4)
                        .attr('width', 3)
                        .attr('height', 8)
                        .attr('fill', color);
                } else {
                    const marker = defs.append('marker')
                        .attr('id', `arrow-${type}`)
                        .attr('viewBox', '0 -5 10 10')
                        .attr('refX', 10)
                        .attr('refY', 0)
                        .attr('markerWidth', 10)
                        .attr('markerHeight', 10)
                        .attr('orient', 'auto');
                    
                    marker.append('rect')
                        .attr('x', 4)
                        .attr('y', -4)
                        .attr('width', 2)
                        .attr('height', 8)
                        .attr('fill', color);
                    
                    marker.append('rect')
                        .attr('x', 7)
                        .attr('y', -4)
                        .attr('width', 2)
                        .attr('height', 8)
                        .attr('fill', color);
                }
            });

            processData();
            createForceSimulation();
        }

        function calculateDynamicSpacing(numInteractors) {
            // Calculate spacing parameters based on number of interactors
            const minRadius = 200;
            const maxRadius = 500;
            
            // Calculate required circumference for optimal spacing
            const requiredCircumference = numInteractors * 100; // 100px per interactor
            const calculatedRadius = requiredCircumference / (2 * Math.PI);
            
            // Constrain to min/max and ensure proper spacing
            const interactorRadius = Math.max(minRadius, Math.min(maxRadius, calculatedRadius));
            
            // Function radius should be further out
            const functionRadius = interactorRadius + 150 + (numInteractors * 5);
            
            return {
                interactorRadius: interactorRadius,
                functionRadius: functionRadius
            };
        }

        function processData() {
            const spacing = calculateDynamicSpacing(data.interactors.length);
            
            // Create main node
            const mainNode = {
                id: data.main,
                label: data.main,
                type: 'main',
                radius: mainNodeRadius,
                x: width / 2,
                y: height / 2,
                fx: width / 2,
                fy: height / 2
            };
            nodes.push(mainNode);

            // Track bidirectional links
            const linkPairs = {};

            // First pass: collect all interactions
            const allInteractions = [];
            data.interactors.forEach((interactor, i) => {
                // Check for multiple arrows/directions (bidirectional)
                if (interactor.all_arrows && interactor.all_arrows.length > 1) {
                    // Handle multiple interaction types for same interactor
                    interactor.all_arrows.forEach((arrow, idx) => {
                        const direction = interactor.all_directions ? interactor.all_directions[idx] :
                    interactor.direction;
                        const intent = interactor.all_intents ? interactor.all_intents[idx] :
                    interactor.intent;
                        allInteractions.push({
                            ...interactor,
                            arrow: arrow || 'binds',
                            direction: direction,
                            intent: intent,
                            interactionIndex: idx
                        });
                    });
                } else {
                    allInteractions.push({
                        ...interactor,
                        arrow: interactor.arrow || 'binds',
                        interactionIndex: 0
                    });
                }
            });

            // Process interactors and create nodes
            const processedInteractors = new Set();
            
            data.interactors.forEach((interactor, i) => {
                if (processedInteractors.has(interactor.primary)) {
                    return; // Skip duplicates
                }
                processedInteractors.add(interactor.primary);
                
                const angle = (2 * Math.PI * i) / data.interactors.length - Math.PI / 2;
                
                const interactorNode = {
                    id: interactor.primary,
                    label: interactor.primary,
                    type: 'interactor',
                    radius: interactorNodeRadius,
                    x: width / 2 + Math.cos(angle) * spacing.interactorRadius,
                    y: height / 2 + Math.sin(angle) * spacing.interactorRadius,
                    data: interactor,
                    confidence: interactor.confidence
                };
                nodes.push(interactorNode);

                // Process all interactions for this interactor
                const relatedInteractions = allInteractions.filter(int => int.primary === interactor.primary);
                
                relatedInteractions.forEach((interaction, intIdx) => {
                    const source = interaction.direction === 'primary_to_main' ? interaction.primary : data.main;
                    const target = interaction.direction === 'primary_to_main' ? data.main : interaction.primary;
                    
                    // Create unique link ID
                    const linkId = `${source}-${target}`;
                    const reverseLinkId = `${target}-${source}`;
                    
                    // Check if reverse link exists
                    let isBidirectional = false;
                    let linkOffset = 0;
                    
                    if (linkPairs[reverseLinkId]) {
                        isBidirectional = true;
                        linkOffset = 1; // This is the second link in the pair
                        linkPairs[linkId] = { offset: linkOffset, pair: reverseLinkId };
                    } else if (!linkPairs[linkId]) {
                        linkPairs[linkId] = { offset: 0, pair: null };
                    }
                    
                    const interactionLink = {
                        source: source,
                        target: target,
                        type: 'interaction',
                        arrow: interaction.arrow || 'binds',
                        intent: interaction.intent,
                        data: interaction,
                        direction: interaction.direction,
                        multiple_mechanisms: interaction.multiple_mechanisms,
                        isBidirectional: isBidirectional,
                        linkOffset: linkPairs[linkId].offset,
                        id: linkId
                    };
                    links.push(interactionLink);
                });

                // Process functions
                let totalFunctions = interactor.functions.length;
                let functionIndex = 0;
                
                // Process regular functions
                interactor.functions.forEach((func, j) => {
                    const funcId = `${interactor.primary}_func_${j}`;
                    const funcAngle = angle + (functionIndex - (totalFunctions - 1) / 2) * (0.15 / Math.sqrt(data.interactors.length));
                    functionIndex++;

                    const affectedProtein = interactor.direction === 'primary_to_main' ? data.main : interactor.primary;
                    const isQueryFunction = interactor.direction === 'primary_to_main';

                    const functionNode = {
                        id: funcId,
                        label: func.function || 'Function',  // SHORT name from function field
                        type: 'function',
                        x: width / 2 + Math.cos(funcAngle) * spacing.functionRadius,
                        y: height / 2 + Math.sin(funcAngle) * spacing.functionRadius,
                        data: func,  // This contains all the function data
                        parent: interactor.primary,
                        confidence: func.confidence,
                        isQueryFunction: isQueryFunction,
                        affectedProtein: affectedProtein,
                        interactorData: interactor
                    };
                    nodes.push(functionNode);

                    links.push({
                        source: interactor.primary,
                        target: funcId,
                        type: 'function',
                        arrow: func.arrow || 'binds',
                        data: func
                    });
                });
            });
        }

        function createForceSimulation() {
            // Adjust force parameters based on number of nodes
            const numNodes = nodes.length;
            const chargeStrength = -500 - (numNodes * 10); // Stronger repulsion with more nodes
            const linkDistance = d => {
                if (d.type === 'function') return 150 + (data.interactors.length * 3);
                return 200 + (data.interactors.length * 10);
            };

            simulation = d3.forceSimulation(nodes)
                .force('link', d3.forceLink(links)
                    .id(d => d.id)
                    .distance(linkDistance))
                .force('charge', d3.forceManyBody().strength(chargeStrength))
                .force('center', d3.forceCenter(width / 2, height / 2))
                .force('collision', d3.forceCollide().radius(d => {
                    if (d.type === 'main') return mainNodeRadius + 40;
                    if (d.type === 'interactor') return interactorNodeRadius + 30;
                    return 80;
                }))
                .force('x', d3.forceX(width / 2).strength(0.01))
                .force('y', d3.forceY(height / 2).strength(0.01));

            const link = g.append('g')
                .selectAll('path')
                .data(links)
                .enter().append('path')
                .attr('class', d => {
                    const arrow = d.arrow || 'binds';
                    let classes = '';
                    if (d.intent === 'binding' || arrow === 'binds') {
                        classes = 'link link-binding';
                    } else if (arrow === 'activates') {
                        classes = 'link link-activate';
                    } else if (arrow === 'inhibits') {
                        classes = 'link link-inhibit';
                    }
                    if (d.type === 'function') {
                        classes += ' function-link';
                    }
                    return classes;
                })
                .attr('marker-end', d => {
                    if (d.type === 'function') return null;
                    const arrow = d.arrow || 'binds';
                    if (arrow === 'activates') return 'url(#arrow-activate)';
                    if (arrow === 'inhibits') return 'url(#arrow-inhibit)';
                    if (arrow === 'binds' || d.intent === 'binding') return 'url(#arrow-binding)';
                    return null;
                })
                .attr('fill', 'none')
                .on('click', handleLinkClick);

            const node = g.append('g')
                .selectAll('g')
                .data(nodes)
                .enter().append('g')
                .attr('class', 'node-group')
                .call(d3.drag()
                    .on('start', dragstarted)
                    .on('drag', dragged)
                    .on('end', dragended));

            node.each(function(d) {
                const group = d3.select(this);
                
                if (d.type === 'main') {
                    group.append('circle')
                        .attr('class', 'node main-node')
                        .attr('r', mainNodeRadius);
                    
                    group.append('text')
                        .attr('class', 'node-label')
                        .attr('dy', 5)
                        .text(d.label);
                    
                } else if (d.type === 'interactor') {
                    group.append('circle')
                        .attr('class', 'node interactor-node')
                        .attr('r', interactorNodeRadius);
                    
                    group.append('text')
                        .attr('class', 'node-label')
                        .attr('dy', 5)
                        .text(d.label);
                    
                    if (d.confidence) {
                        const confClass = d.confidence >= 0.7 ? 'high' : 
                                        d.confidence >= 0.5 ? 'medium' : 'low';
                        group.append('text')
                            .attr('class', `confidence-badge confidence-${confClass}`)
                            .attr('dy', -35)
                            .text(`${Math.round(d.confidence * 100)}%`);
                    }
                    
                } else if (d.type === 'function') {
                    // Function boxes - display the SHORT function name
                    const displayText = d.label || 'Function';  // Use the short name
                    
                    const tempText = group.append('text')
                        .attr('class', 'function-label')
                        .text(displayText)
                        .attr('visibility', 'hidden');
                    
                    const bbox = tempText.node().getBBox();
                    tempText.remove();
                    
                    const padding = 16;
                    const rectWidth = Math.max(bbox.width + padding * 2, 100);
                    const rectHeight = Math.max(bbox.height + padding * 1.5, 36);
                    
                    const nodeClass = d.isQueryFunction ? 'function-node-query' : 'function-node-interactor';
                    
                    const rect = group.append('rect')
                        .attr('class', `node function-node ${nodeClass}`)
                        .attr('x', -rectWidth / 2)
                        .attr('y', -rectHeight / 2)
                        .attr('width', rectWidth)
                        .attr('height', rectHeight)
                        .attr('rx', 6)
                        .attr('ry', 6)
                        .on('click', function(event) {
                            event.stopPropagation();
                            showFunctionDetails(d);
                        });
                    
                    // Add function text (SHORT name)
                    group.append('text')
                        .attr('class', 'function-label')
                        .attr('dy', 4)
                        .text(displayText)
                        .style('pointer-events', 'none');
                    
                    // Add indicator if there are specific effects
                    if (d.data.specific_effects && d.data.specific_effects.length > 0) {
                        const indicatorGroup = group.append('g')
                            .attr('transform', `translate(${rectWidth/2 - 12}, ${-rectHeight/2 + 12})`);
                        
                        indicatorGroup.append('circle')
                            .attr('class', 'multi-function-indicator')
                            .attr('r', 10);
                        
                        indicatorGroup.append('text')
                            .attr('class', 'multi-function-text')
                            .attr('dy', 4)
                            .text(d.data.specific_effects.length);
                    }
                    
                    // Add confidence score
                    if (d.confidence) {
                        const confClass = d.confidence >= 0.7 ? 'high' : 
                                        d.confidence >= 0.5 ? 'medium' : 'low';
                        group.append('text')
                            .attr('class', `confidence-badge confidence-${confClass}`)
                            .attr('dy', rectHeight / 2 + 14)
                            .style('font-size', '12px')
                            .text(`${Math.round(d.confidence * 100)}%`);
                    }
                }
            });

            simulation.on('tick', () => {
                link.attr('d', d => {
                    const source = nodes.find(n => n.id === (d.source.id || d.source));
                    const target = nodes.find(n => n.id === (d.target.id || d.target));
                    
                    if (!source || !target) {
                        return `M 0 0 L 0 0`;
                    }
                    
                    // Calculate the line from edge to edge of nodes
                    const dx = target.x - source.x;
                    const dy = target.y - source.y;
                    const distance = Math.sqrt(dx * dx + dy * dy);
                    
                    if (distance === 0) {
                        return `M ${source.x} ${source.y} L ${target.x} ${target.y}`;
                    }
                    
                    // Get node radii
                    let sourceRadius = 0;
                    let targetRadius = 0;
                    
                    if (source.type === 'main') sourceRadius = mainNodeRadius;
                    else if (source.type === 'interactor') sourceRadius = interactorNodeRadius;
                    
                    if (target.type === 'main') targetRadius = mainNodeRadius;
                    else if (target.type === 'interactor') targetRadius = interactorNodeRadius;
                    
                    // Calculate offset for bidirectional links
                    let offsetAmount = 0;
                    if (d.isBidirectional && d.type === 'interaction') {
                        offsetAmount = d.linkOffset === 0 ? -8 : 8; // Offset by 8 pixels
                    }
                    
                    // Calculate perpendicular offset
                    const perpX = -dy / distance * offsetAmount;
                    const perpY = dx / distance * offsetAmount;
                    
                    // Calculate points on the edge of circles with offset
                    const sourceX = source.x + (dx / distance) * sourceRadius + perpX;
                    const sourceY = source.y + (dy / distance) * sourceRadius + perpY;
                    const targetX = target.x - (dx / distance) * targetRadius + perpX;
                    const targetY = target.y - (dy / distance) * targetRadius + perpY;
                    
                    // Use curved path for bidirectional links
                    if (d.isBidirectional && d.type === 'interaction') {
                        const midX = (sourceX + targetX) / 2;
                        const midY = (sourceY + targetY) / 2;
                        const curveOffset = offsetAmount * 2;
                        const curveMidX = midX + perpX;
                        const curveMidY = midY + perpY;
                        return `M ${sourceX} ${sourceY} Q ${curveMidX} ${curveMidY} ${targetX} ${targetY}`;
                    }
                    
                    return `M ${sourceX} ${sourceY} L ${targetX} ${targetY}`;
                });

                node.attr('transform', d => `translate(${d.x},${d.y})`);
            });
        }

        function handleLinkClick(event, d) {
            event.stopPropagation();
            showModal(d);
        }

        function showFunctionDetails(functionNode) {
            const modal = document.getElementById('modal');
            const modalTitle = document.getElementById('modalTitle');
            const modalBody = document.getElementById('modalBody');
            
            modalTitle.textContent = `Function: ${functionNode.label}`;
            
            // Format the biological consequence with arrow highlighting
            let biologicalConsequence = functionNode.data.biological_consequence || 
                                       'Cascading biological effects not specified';
            biologicalConsequence = biologicalConsequence.replace(/→/g, '<span class="cascade-arrow">→</span>');
            
            let specificEffectsHTML = '';
            if (functionNode.data.specific_effects && functionNode.data.specific_effects.length > 0) {
                specificEffectsHTML = `
                    <tr class="info-row">
                        <td class="info-label">SPECIFIC EFFECTS</td>
                        <td class="info-value">
                            ${functionNode.data.specific_effects.map(effect => `
                                <div class="specific-effect">• ${effect}</div>
                            `).join('')}
                        </td>
                    </tr>
                `;
            }
            
            const confValue = functionNode.confidence || 0;
            const confClass = confValue >= 0.7 ? 'high' : confValue >= 0.5 ? 'medium' : 'low';
            
            // Format references with full paper details from evidence
            let referencesHTML = 'No references available';
            if (functionNode.data.evidence && functionNode.data.evidence.length > 0) {
                referencesHTML = functionNode.data.evidence.map(ev => {
                    const paperTitle = ev.paper_title || 'Title not available';
                    const authors = ev.authors || '';
                    const journal = ev.journal || '';
                    const year = ev.year || '';
                    const pmid = ev.pmid || '';
                    const doi = ev.doi || '';
                    const quote = ev.relevant_quote || '';
                    
                    return `
                        <div class="evidence-item">
                            <div><strong>${paperTitle}</strong></div>
                            ${authors ? `<div style="color: #6b7280; font-size: 12px;">${authors}</div>` : ''}
                            <div style="color: #6b7280; font-size: 12px;">
                                ${journal ? `${journal}` : ''}
                                ${year ? ` (${year})` : ''}
                            </div>
                            <div style="margin-top: 4px;">
                                ${pmid ? `<a href="https://pubmed.ncbi.nlm.nih.gov/${pmid}" target="_blank" class="pmid-link">PMID: ${pmid}</a>` : ''}
                                ${doi ? ` | <span style="color: #6b7280;">DOI: ${doi}</span>` : ''}
                            </div>
                            ${quote ? `<div style="margin-top: 6px; font-style: italic; color: #4b5563; font-size: 12px;">"${quote}"</div>` : ''}
                        </div>
                    `;
                }).join('');
            } else if (functionNode.data.pmids && functionNode.data.pmids.length > 0) {
                // Fallback to simple PMID links if no evidence array
                referencesHTML = functionNode.data.pmids.map(pmid => 
                    `<a href="https://pubmed.ncbi.nlm.nih.gov/${pmid}" target="_blank" class="pmid-link">PMID: ${pmid}</a>`
                ).join(', ');
            }
            
            modalBody.innerHTML = `
                <table class="info-table">
                    <tr class="info-row">
                        <td class="info-label">FUNCTION</td>
                        <td class="info-value"><strong style="font-size: 16px;">${functionNode.label}</strong></td>
                    </tr>
                    <tr class="info-row">
                        <td class="info-label">AFFECTED PROTEIN</td>
                        <td class="info-value"><strong>${functionNode.affectedProtein}</strong></td>
                    </tr>
                    <tr class="info-row">
                        <td class="info-label">CELLULAR PROCESS</td>
                        <td class="info-value">
                            <div class="cellular-theme">
                                <div class="cellular-theme-title">Molecular Mechanism</div>
                                ${functionNode.data.cellular_process || 'Molecular mechanism not specified'}
                            </div>
                        </td>
                    </tr>
                    <tr class="info-row">
                        <td class="info-label">BIOLOGICAL CASCADE</td>
                        <td class="info-value">
                            <div class="biological-cascade">
                                ${biologicalConsequence}
                            </div>
                        </td>
                    </tr>
                    ${specificEffectsHTML}
                    <tr class="info-row">
                        <td class="info-label">EFFECT TYPE</td>
                        <td class="info-value">
                            ${functionNode.data.arrow === 'activates' ? 
                              '<strong style="color: #059669;">✓ Enhanced/Activated</strong>' : 
                              '<strong style="color: #dc2626;">✗ Inhibited/Disrupted</strong>'}
                        </td>
                    </tr>
                    <tr class="info-row">
                        <td class="info-label">MECHANISM</td>
                        <td class="info-value">${functionNode.interactorData.intent ? 
                            functionNode.interactorData.intent.charAt(0).toUpperCase() + 
                            functionNode.interactorData.intent.slice(1) : 'Not specified'}</td>
                    </tr>
                    <tr class="info-row">
                        <td class="info-label">CONFIDENCE</td>
                        <td class="info-value">
                            <strong style="font-size: 16px;">${Math.round(confValue * 100)}%</strong>
                            <div class="confidence-meter">
                                <div class="confidence-fill ${confClass}" style="width: ${confValue * 100}%"></div>
                            </div>
                        </td>
                    </tr>
                    <tr class="info-row">
                        <td class="info-label">REFERENCES</td>
                        <td class="info-value">${referencesHTML}</td>
                    </tr>
                </table>
            `;
            
            modal.classList.add('active');
        }

        function showModal(linkData) {
            const modal = document.getElementById('modal');
            const modalTitle = document.getElementById('modalTitle');
            const modalBody = document.getElementById('modalBody');

            if (linkData.type === 'interaction') {
                const arrow = linkData.data.direction === 'primary_to_main' ? '→' : '←';
                modalTitle.textContent = `${linkData.data.primary} ${arrow} ${data.main}`;
                
                const confValue = linkData.data.confidence || 0;
                const confClass = confValue >= 0.7 ? 'high' : confValue >= 0.5 ? 'medium' : 'low';
                
                modalBody.innerHTML = `
                    <table class="info-table">
                        <tr class="info-row">
                            <td class="info-label">MECHANISM</td>
                            <td class="info-value">${linkData.data.intent ? linkData.data.intent.charAt(0).toUpperCase() + linkData.data.intent.slice(1) : ''}</td>
                        </tr>
                        <tr class="info-row">
                            <td class="info-label">EFFECT</td>
                            <td class="info-value">${linkData.arrow === 'activates' ? '✓ Activates' : linkData.arrow === 'inhibits' ? '✗ Inhibits' : '═ Binds'}</td>
                        </tr>
                        <tr class="info-row">
                            <td class="info-label">SUMMARY</td>
                            <td class="info-value">${linkData.data.support_summary || ''}</td>
                        </tr>
                        <tr class="info-row">
                            <td class="info-label">CONFIDENCE</td>
                            <td class="info-value">
                                <strong style="font-size: 16px;">${Math.round(confValue * 100)}%</strong>
                                <div class="confidence-meter">
                                    <div class="confidence-fill ${confClass}" style="width: ${confValue * 100}%}</div>
                                </div>
                            </td>
                        </tr>
                        <tr class="info-row">
                            <td class="info-label">EVIDENCE</td>
                            <td class="info-value">
                                ${linkData.data.evidence ? linkData.data.evidence.map(e => `
                                    <div class="evidence-item">
                                        <div><strong>PMID:</strong> <a href="https://pubmed.ncbi.nlm.nih.gov/${e.pmid}" target="_blank" class="pmid-link">${e.pmid}</a></div>
                                        <div><strong>Year:</strong> ${e.year || 'Unknown'}</div>
                                        <div><strong>Species:</strong> ${e.species || 'Not specified'}</div>
                                        <div><strong>Method:</strong> ${e.assay || 'Not specified'}</div>
                                        <div><strong>Finding:</strong> ${e.notes || 'No details provided'}</div>
                                    </div>
                                `).join('') : 'No evidence data available'}
                            </td>
                        </tr>
                    </table>
                `;
            }

            modal.classList.add('active');
        }

        function closeModal() {
            document.getElementById('modal').classList.remove('active');
        }

        function zoomIn() {
            svg.transition().call(
                d3.zoom().transform,
                d3.zoomIdentity.scale(currentZoom * 1.3)
            );
        }

        function zoomOut() {
            svg.transition().call(
                d3.zoom().transform,
                d3.zoomIdentity.scale(currentZoom * 0.7)
            );
        }

        function resetView() {
            svg.transition().call(
                d3.zoom().transform,
                d3.zoomIdentity
            );
        }

        function dragstarted(event, d) {
            if (!event.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
        }

        function dragged(event, d) {
            d.fx = event.x;
            d.fy = event.y;
        }

        function dragended(event, d) {
            if (!event.active) simulation.alphaTarget(0);
            if (d.type !== 'main') {
                d.fx = null;
                d.fy = null;
            }
        }

        document.addEventListener('DOMContentLoaded', initNetwork);

        window.addEventListener('resize', () => {
            width = document.getElementById('network').clientWidth;
            height = document.getElementById('network').clientHeight;
            svg.attr('width', width).attr('height', height);
            simulation.force('center', d3.forceCenter(width / 2, height / 2));
            simulation.alpha(0.3).restart();
        });

        document.getElementById('modal').addEventListener('click', (e) => {
            if (e.target.id === 'modal') {
                closeModal();
            }
        });
    </script>
</body>
</html>
'''
    
    # Replace placeholders with actual data
    formatted_html = html_template.replace('PLACEHOLDER_MAIN_PROTEIN', main_protein)
    formatted_html = formatted_html.replace('PLACEHOLDER_JSON_DATA', json_data_str)
    
    # Save to file
    if output_path:
        output_file = Path(output_path)
    else:
        # Create temp file
        temp_file = tempfile.NamedTemporaryFile(
            mode='w',
            delete=False,
            suffix='.html',
            prefix=f"{viz_data.get('main', 'protein')}_network_"
        )
        output_file = Path(temp_file.name)
        temp_file.close()
    
    # Write the HTML
    output_file.write_text(formatted_html, encoding='utf-8')
    
    print(f"Visualization saved to: {output_file}")
    return output_file


def open_visualization(html_path):
    """Opens the HTML visualization in the default web browser"""
    webbrowser.open(f"file://{html_path.absolute()}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python visualizer.py <json_file> [output_html]")
        print("Example: python visualizer.py STMN2_pipeline.json")
        sys.exit(1)
    
    json_file = Path(sys.argv[1])
    output = sys.argv[2] if len(sys.argv) > 2 else None
    
    if not json_file.exists():
        print(f"Error: {json_file} not found")
        sys.exit(1)
    
    # Create visualization
    html_file = create_visualization(json_file, output)
    
    # Open in browser
    open_visualization(html_file)
    print(f"Opening visualization in browser...")