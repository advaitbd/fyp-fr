from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import os
import json
import threading
import time
import sys
import uuid
import re
import subprocess
from typing import Dict, Tuple, List, Optional
from datetime import datetime

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import from main project
from utils.source_code_fetcher import fetch_and_flatten_contract
from static_analysis.parse_contract import analyze_contract
from llm_agents.agent_coordinator import AgentCoordinator
from llm_agents.config import ModelConfig
from llm_agents.agents.runner import ExploitRunner
from utils.token_tracker import performance_tracker

app = Flask(__name__, static_folder='build')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Store job states
jobs = {}

# Store uploaded contracts
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    if path != "" and os.path.exists(app.static_folder + '/' + path):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/upload-contract', methods=['POST'])
def upload_contract():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    job_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_{file.filename}")
    file.save(file_path)

    jobs[job_id] = {
        'status': 'uploaded',
        'contract_path': file_path,
        'filename': file.filename
    }

    return jsonify({
        'job_id': job_id,
        'status': 'uploaded',
        'message': 'Contract uploaded successfully'
    })

@app.route('/api/fetch-contract', methods=['POST'])
def fetch_contract():
    data = request.json
    if not data or 'address' not in data or 'network' not in data:
        return jsonify({'error': 'Address and network are required'}), 400

    # Check for save_separate parameter, default to True if not specified
    save_separate = data.get('saveSeparate', True)

    job_id = str(uuid.uuid4())
    output_file = os.path.join(UPLOAD_FOLDER, f"{job_id}_contract.sol")

    try:
        # Start fetching in a separate thread
        jobs[job_id] = {
            'status': 'fetching',
            'address': data['address'],
            'network': data['network']
        }

        threading.Thread(
            target=fetch_contract_thread,
            args=(job_id, data['network'], data['address'], output_file, save_separate)
        ).start()

        return jsonify({
            'job_id': job_id,
            'status': 'fetching',
            'message': 'Contract fetching started'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def fetch_contract_thread(job_id, network, address, output_file, save_separate=True):
    try:
        contract_files_map = fetch_and_flatten_contract(
            network=network,
            contract_address=address,
            output_file=output_file,
            flatten=True,
            save_separate=save_separate
        )

        # Determine if we have a contracts directory for additional analysis
        contracts_dir = None
        if save_separate and contract_files_map:
            # Construct the expected directory name based on the output file
            contracts_dir = f"{os.path.splitext(output_file)[0]}_contracts"
            # Verify the directory exists
            if not os.path.isdir(contracts_dir):
                print(f"Warning: Expected contracts directory {contracts_dir} not found after fetch.")
                contracts_dir = None # Reset if not found

        jobs[job_id].update({
            'status': 'fetched',
            'contract_path': output_file,
            'filename': f"{address}.sol",
            'contracts_dir': contracts_dir,
            'save_separate': save_separate
        })
        socketio.emit('contract_fetched', {'job_id': job_id, 'status': 'fetched'})
    except Exception as e:
        jobs[job_id].update({
            'status': 'error',
            'error': str(e)
        })
        socketio.emit('contract_fetch_error', {'job_id': job_id, 'error': str(e)})

@app.route('/api/analyze', methods=['POST'])
def start_analysis():
    data = request.json
    if not data or 'job_id' not in data:
        return jsonify({'error': 'Job ID is required'}), 400

    job_id = data['job_id']
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    if job['status'] not in ['uploaded', 'fetched']:
        return jsonify({'error': 'Contract not ready for analysis'}), 400

    # Get model configuration
    model_config = ModelConfig(
        analyzer_model=data.get('analyzer_model', 'o3-mini'),
        skeptic_model=data.get('skeptic_model', 'o3-mini'),
        exploiter_model=data.get('exploiter_model', 'o3-mini'),
        generator_model=data.get('generator_model', 'o3-mini'),
        skip_poc_generation=data.get('skip_poc_generation', False),
        export_markdown=data.get('export_markdown', False),
    )

    # Get auto-run configuration
    max_retries = data.get('max_retries', 3)
    try:
        max_retries = int(max_retries)
    except (ValueError, TypeError):
        max_retries = 3  # fallback to default if conversion fails

    auto_run_config = {
        'auto_run': data.get('auto_run', True),
        'max_retries': max_retries
    }

    # Get RAG configuration
    use_rag = data.get('use_rag', True)

    # Update job status
    jobs[job_id].update({
        'status': 'analyzing',
        'model_config': {
            'analyzer_model': model_config.analyzer_model,
            'skeptic_model': model_config.skeptic_model,
            'exploiter_model': model_config.exploiter_model,
            'generator_model': model_config.generator_model,
            'skip_poc_generation': model_config.skip_poc_generation,
            'export_markdown': model_config.export_markdown,
            'context_model': model_config.context_model,
        },
        'auto_run_config': auto_run_config,
        'use_rag': use_rag
    })

    # Start analysis in a separate thread
    threading.Thread(
        target=analyze_thread,
        args=(job_id, job['contract_path'], model_config, auto_run_config, use_rag)
    ).start()

    return jsonify({
        'job_id': job_id,
        'status': 'analyzing',
        'message': 'Analysis started'
    })

def analyze_thread(job_id, contract_path, model_config, auto_run_config, use_rag=True):
    try:
        # Emit analysis started event
        print(f"Emitting analysis_started event for job {job_id}")
        socketio.emit('analysis_started', {'job_id': job_id})

        # Start performance tracking
        performance_tracker.reset()
        performance_tracker.start_stage("initialization")

        # Get job details
        job = jobs.get(job_id, {})

        # --- REMOVED log_code_analysis from here ---

        # Set run configuration for tracking
        run_config = {
            "analyzer_model": model_config.analyzer_model,
            "skeptic_model": model_config.skeptic_model,
            "exploiter_model": model_config.exploiter_model,
            "generator_model": model_config.generator_model,
            "context_model": model_config.context_model,
            "all_models": None, # Assuming this is set elsewhere or default
            "use_rag": use_rag,
            "skip_poc": model_config.skip_poc_generation,
            "auto_run": auto_run_config['auto_run']
        }
        performance_tracker.set_run_config(run_config)

        # Static Analysis
        print(f"Emitting agent_active event for static_analyzer for job {job_id}")
        performance_tracker.start_stage("static_analysis")
        socketio.emit('agent_active', {
            'job_id': job_id,
            'agent': 'static_analyzer',
            'status': 'Starting static analysis',
            'detail': 'Parsing contract code and preparing for analysis'
        })

        # Determine the primary contract file or directory for Slither analysis
        analysis_target = contract_path # Default to flattened file
        if job.get('contracts_dir') and os.path.isdir(job['contracts_dir']):
             # For multi-file projects, pass the directory to Slither
             analysis_target = job['contracts_dir']
             print(f"Using directory for static analysis: {analysis_target}")
        else:
             print(f"Using single file for static analysis: {analysis_target}")

        # Emit status update for parsing
        socketio.emit('agent_status', {
            'job_id': job_id,
            'agent': 'static_analyzer',
            'status': 'Parsing contract(s)',
            'detail': f'Analyzing target: {os.path.basename(analysis_target)}'
        })

        # Run static analysis
        socketio.emit('agent_status', {
            'job_id': job_id,
            'agent': 'static_analyzer',
            'status': 'Running Slither',
            'detail': 'Executing static analysis tools to detect common vulnerabilities'
        })

        try:
            # Pass the correct target (file or directory) to analyze_contract
            function_details, call_graph, detector_results = analyze_contract(analysis_target)
        except Exception as slither_error:
            print(f"Error during Slither analysis on {analysis_target}: {slither_error}")
            # Attempt analysis on the flattened file as a fallback if directory analysis failed
            if analysis_target != contract_path and os.path.exists(contract_path):
                print(f"Attempting fallback analysis on flattened file: {contract_path}")
                try:
                     function_details, call_graph, detector_results = analyze_contract(contract_path)
                except Exception as fallback_error:
                     print(f"Fallback analysis on flattened file failed: {fallback_error}")
                     raise fallback_error # Re-raise the error if fallback also fails
            else:
                raise slither_error # Re-raise the original error if no fallback possible

        # Count the number of functions
        function_count = len(function_details) if function_details else 0

        # Get summary of detector results if available
        detector_count = 0
        if detector_results:
            try:
                # Try to count results from various detectors
                detector_count = sum(len(items) for _, items in detector_results.items() if isinstance(items, list))
            except:
                # Fallback: just indicate we have some results
                detector_count = 1 if detector_results else 0

        # Emit status with analysis summary
        socketio.emit('agent_status', {
            'job_id': job_id,
            'agent': 'static_analyzer',
            'status': 'Analysis complete',
            'detail': f'Found {function_count} functions and {detector_count} potential issues'
        })

        print(f"Emitting agent_complete event for static_analyzer for job {job_id}")
        socketio.emit('agent_complete', {
            'job_id': job_id,
            'agent': 'static_analyzer',
            'result': f'Analyzed {function_count} functions'
        })

        # Read contract source (use flattened file for LLM context, or handle multi-file appropriately later)
        with open(contract_path, "r", encoding="utf-8") as f:
            source_code = f.read()

        # Prepare contract info for AgentCoordinator
        contract_info = {
            "function_details": function_details,
            "call_graph": call_graph,
            "source_code": source_code, # Pass flattened source for main context
            "detector_results": detector_results,
        }

        # Add contracts directory for inter-contract analysis if available
        if job.get('contracts_dir') and os.path.isdir(job['contracts_dir']):
            contracts_dir = job['contracts_dir'] # Assign for later use
            contract_info["contracts_dir"] = contracts_dir

            # Determine file count for display (count .sol files in the directory)
            try:
                sol_files = []
                for root, _, files in os.walk(contracts_dir):
                     sol_files.extend([os.path.join(root, f) for f in files if f.endswith('.sol')])
                contract_count = len(sol_files)
            except Exception as count_error:
                 print(f"Error counting files in {contracts_dir}: {count_error}")
                 contract_count = 'Unknown'


            # Emit initial status that LLM ProjectContextAgent is starting
            socketio.emit('agent_active', {
                'job_id': job_id,
                'agent': 'project_context_llm',
                'status': 'Starting LLM-powered inter-contract analysis',
                'detail': f"LLM-powered ProjectContextAgent will analyze {contract_count} contracts for relationships"
            })

            # After a brief delay, update with more details
            socketio.emit('agent_status', {
                'job_id': job_id,
                'agent': 'project_context_llm',
                'status': 'LLM analyzing contract relationships',
                'detail': f"Autonomously exploring contracts in {os.path.basename(contracts_dir)}"
            })
        else:
            contracts_dir = None # Ensure contracts_dir is None if not applicable

        # Initialize agent coordinator using SocketIOAgentCoordinator to emit events
        coordinator = SocketIOAgentCoordinator(
            job_id,
            model_config=model_config,
            use_rag=use_rag
        )

        # Run the main analysis pipeline
        results = coordinator.analyze_contract(contract_info, auto_run_config=auto_run_config)

        # Update job status
        jobs[job_id].update({
            'status': 'completed',
            'results': results
        })

        # --- MOVED log_code_analysis to here, AFTER the main pipeline ---
        # Determine the final list of files analyzed for accurate line counting
        files_analyzed_final = []
        if contracts_dir and os.path.isdir(contracts_dir):
            try:
                # Recursively find all .sol files in the directory
                for root, _, files in os.walk(contracts_dir):
                    for file in files:
                        if file.endswith('.sol'):
                            files_analyzed_final.append(os.path.join(root, file))
                if not files_analyzed_final: # Fallback if dir is empty
                    if os.path.exists(contract_path):
                        files_analyzed_final.append(contract_path)
                        print(f"Warning: Contracts directory {contracts_dir} is empty. Logging LOC for flattened file: {contract_path}")
                    else:
                        print(f"Error: Contracts directory {contracts_dir} is empty and flattened file {contract_path} not found.")
                else:
                    print(f"Logging analysis metrics for {len(files_analyzed_final)} files from directory: {contracts_dir}")
            except Exception as e:
                print(f"Error walking contracts directory {contracts_dir} for final logging: {e}. Falling back to flattened file.")
                files_analyzed_final = [contract_path] if os.path.exists(contract_path) else []
        elif os.path.exists(contract_path):
            files_analyzed_final.append(contract_path)
            print(f"Logging analysis metrics for single file: {contract_path}")
        else:
             print(f"Error: No valid contract path or directory found for final logging for job {job_id}. Contract path: {contract_path}")

        # Log code analysis using the determined files BEFORE getting the summary
        if files_analyzed_final:
            performance_tracker.log_code_analysis(files_analyzed_final)
            print(f"Final LOC count: {performance_tracker.total_lines} lines across {len(files_analyzed_final)} files.")
        else:
             print("Warning: No files found to log for performance analysis.")
             # Ensure metrics are zeroed if no files found
             performance_tracker.contract_files = []
             performance_tracker.total_lines = 0


        # Get performance metrics
        metrics = None
        if performance_tracker:
            # End any current stage (e.g., results_reporting from coordinator)
            if performance_tracker.current_stage is not None:
                performance_tracker.end_stage()

            # Get metrics as dictionary (now includes accurate LOC)
            metrics = performance_tracker.get_performance_summary()

            # Save metrics to a JSON file for reference
            metrics_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.makedirs(metrics_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            metrics_file = os.path.join(
                metrics_dir,
                f"performance_metrics_job_{job_id}_{timestamp}.json"
            )

            try:
                with open(metrics_file, 'w') as f:
                    json.dump(metrics, f, indent=2)
                print(f"Saved performance metrics to {metrics_file}")
            except Exception as e:
                print(f"Error saving performance metrics to {metrics_file}: {e}")

            # Save metrics to job data
            jobs[job_id]['performance_metrics'] = metrics

        # Emit analysis completed event
        print(f"Emitting analysis_complete event for job {job_id}")
        socketio.emit('analysis_complete', {
            'job_id': job_id,
            'status': 'completed',
            'vulnerabilities_count': len(results.get('rechecked_vulnerabilities', [])),
            'pocs_count': len(results.get('generated_pocs', [])),
            'performance_metrics': metrics
        })

    except Exception as e:
        print(f"Error in analysis thread for job {job_id}: {str(e)}")
        # Ensure job_id exists before updating
        if job_id in jobs:
            jobs[job_id].update({
                'status': 'error',
                'error': str(e)
            })
        # Emit error event
        socketio.emit('analysis_error', {'job_id': job_id, 'error': str(e)})
        # Optionally re-raise or log traceback
        import traceback
        traceback.print_exc()


@app.route('/api/status/<job_id>', methods=['GET'])
def get_status(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    return jsonify({
        'job_id': job_id,
        'status': jobs[job_id]['status']
    })

@app.route('/api/results/<job_id>', methods=['GET'])
def get_results(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    if job['status'] != 'completed':
        return jsonify({'error': 'Analysis not completed'}), 400

    # Include performance metrics if available
    response = {
        'job_id': job_id,
        'status': 'completed',
        'results': job['results']
    }

    if 'performance_metrics' in job:
        response['performance_metrics'] = job['performance_metrics']

    return jsonify(response)

@app.route('/api/performance-metrics/<job_id>', methods=['GET'])
def get_performance_metrics(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    if job['status'] != 'completed':
        return jsonify({'error': 'Analysis not completed'}), 400

    # Check if performance metrics are available
    if 'performance_metrics' not in job:
        return jsonify({'error': 'Performance metrics not available for this job'}), 404

    return jsonify({
        'job_id': job_id,
        'performance_metrics': job['performance_metrics']
    })

@app.route('/api/demo', methods=['POST'])
def start_demo():
    """
    Starts a demo job that mimics the full multi-file flow with canned results.
    """
    job_id = str(uuid.uuid4())
    demo_contracts_dir = "/Users/advait/Desktop/NTU/fyp-fr/examples/MultiContract"
    jobs[job_id] = {
        'status': 'fetching',
        'contract_path': None,
        'contracts_dir': demo_contracts_dir,
        'filename': 'DemoProject',
        'save_separate': True
    }
    threading.Thread(target=demo_thread, args=(job_id, demo_contracts_dir)).start()
    return jsonify({'job_id': job_id, 'status': 'fetching', 'message': 'Demo started'})

def demo_thread(job_id, contracts_dir):
    import random
    # Step 1: Mimic contract fetch
    time.sleep(1)
    jobs[job_id].update({
        'status': 'fetched',
        'contract_path': os.path.join(contracts_dir, 'MainContract.sol'),
        'contracts_dir': contracts_dir,
        'filename': 'MainContract.sol',
        'save_separate': True
    })
    socketio.emit('contract_fetched', {'job_id': job_id, 'status': 'fetched'})

    # Step 2: Mimic analysis start
    time.sleep(1)
    jobs[job_id]['status'] = 'analyzing'
    socketio.emit('analysis_started', {'job_id': job_id})

    # Step 3: Static Analyzer
    time.sleep(1)
    socketio.emit('agent_active', {
        'job_id': job_id,
        'agent': 'static_analyzer',
        'status': 'Starting static analysis',
        'detail': 'Parsing contract code and preparing for analysis'
    })
    time.sleep(1)
    socketio.emit('agent_status', {
        'job_id': job_id,
        'agent': 'static_analyzer',
        'status': 'Analysis complete',
        'detail': 'Found 12 functions and 2 potential issues'
    })
    socketio.emit('agent_complete', {
        'job_id': job_id,
        'agent': 'static_analyzer',
        'result': 'Analyzed 12 functions'
    })

    # Step 4: Project Context LLM (RAG)
    time.sleep(1)
    socketio.emit('agent_active', {
        'job_id': job_id,
        'agent': 'project_context_llm',
        'status': 'Starting LLM-powered inter-contract analysis',
        'detail': 'LLM-powered ProjectContextAgent will analyze 3 contracts for relationships'
    })
    time.sleep(1)
    socketio.emit('agent_status', {
        'job_id': job_id,
        'agent': 'project_context_llm',
        'status': 'LLM analyzing contract relationships',
        'detail': 'Autonomously exploring contracts in MultiContract'
    })
    # Emit fake RAG/project context insights
    socketio.emit('project_context_insights', {
        'job_id': job_id,
        'details': {
            'insights': [
                'Contract A calls Contract B in fallback.',
                'Contract C manages access control for A and B.'
            ],
            'dependencies': [
                'A depends on B',
                'B depends on C'
            ],
            'vulnerabilities': [
                'Potential reentrancy between A and B',
                'Unprotected admin function in C'
            ],
            'recommendations': [
                'Add reentrancy guard to A',
                'Restrict admin in C'
            ],
            'important_functions': [
                'A.fallback()',
                'C.setAdmin()'
            ],
            'contract_files': ['A.sol', 'B.sol', 'C.sol'],
            'stats': {
                'total_contracts': 3,
                'total_relationships': 2,
                'total_vulnerabilities': 2,
                'total_recommendations': 2
            }
        }
    })
    socketio.emit('agent_complete', {
        'job_id': job_id,
        'agent': 'project_context_llm',
        'result': 'Analyzed 3 contracts. Found: 2 insights, 2 dependencies, 2 potential vulnerabilities, 2 recommendations'
    })

    # Step 5: Analyzer (with RAG details)
    time.sleep(1)
    socketio.emit('agent_active', {
        'job_id': job_id,
        'agent': 'analyzer',
        'status': 'Initializing vulnerability analysis',
        'detail': 'Loading contract code and preparing for analysis...'
    })
    time.sleep(1)
    socketio.emit('agent_status', {
        'job_id': job_id,
        'agent': 'analyzer',
        'status': 'Analyzing contract code',
        'detail': 'Searching for potential vulnerabilities. Using RAG: True'
    })
    # Emit fake RAG details
    socketio.emit('rag_details', {
        'job_id': job_id,
        'details': [
            {
                'filename': 'known_vulnerabilities/reentrancy/Example.sol',
                'lines_range': '10-30',
                'vuln_categories': ['reentrancy'],
                'content_preview': 'function withdraw() external { ... } // classic reentrancy'
            },
            {
                'filename': 'known_vulnerabilities/access_control/Admin.sol',
                'lines_range': '5-20',
                'vuln_categories': ['access_control'],
                'content_preview': 'function setAdmin(address) public { ... } // missing onlyOwner'
            }
        ]
    })
    time.sleep(1)
    socketio.emit('agent_status', {
        'job_id': job_id,
        'agent': 'analyzer',
        'status': 'Analysis complete',
        'detail': 'Found 2 potential vulnerabilities to investigate'
    })
    socketio.emit('agent_complete', {
        'job_id': job_id,
        'agent': 'analyzer',
        'result': 'Found 2 potential vulnerabilities'
    })

    # Step 6: Skeptic
    time.sleep(1)
    socketio.emit('agent_active', {
        'job_id': job_id,
        'agent': 'skeptic',
        'status': 'Initializing validity check',
        'detail': 'Preparing to verify the reported vulnerabilities...'
    })
    time.sleep(1)
    socketio.emit('agent_status', {
        'job_id': job_id,
        'agent': 'skeptic',
        'status': 'Validating findings',
        'detail': 'Checking validity of 2 potential vulnerabilities'
    })
    time.sleep(1)
    socketio.emit('agent_status', {
        'job_id': job_id,
        'agent': 'skeptic',
        'status': 'Validation complete',
        'detail': 'Found 2 high-confidence vulnerabilities'
    })
    socketio.emit('agent_complete', {
        'job_id': job_id,
        'agent': 'skeptic',
        'result': 'Confirmed 2 vulnerabilities with high confidence'
    })

    # Step 7: Exploiter (for each vuln)
    for i, vuln in enumerate([
        {'vulnerability_type': 'Reentrancy', 'skeptic_confidence': 0.95},
        {'vulnerability_type': 'Access Control', 'skeptic_confidence': 0.92}
    ]):
        time.sleep(1)
        socketio.emit('agent_active', {
            'job_id': job_id,
            'agent': 'exploiter',
            'status': f'Creating exploit plan ({i+1}/2)',
            'detail': f'Planning attack strategy for {vuln["vulnerability_type"]} vulnerability'
        })
        time.sleep(1)
        socketio.emit('agent_status', {
            'job_id': job_id,
            'agent': 'exploiter',
            'status': 'Plan created',
            'detail': f'Exploit plan has 2 setup, 2 execution, and 1 validation steps'
        })
        socketio.emit('agent_complete', {
            'job_id': job_id,
            'agent': 'exploiter',
            'result': f'Created exploit plan for {vuln["vulnerability_type"]}'
        })

        # Step 8: Generator
        time.sleep(1)
        socketio.emit('agent_active', {
            'job_id': job_id,
            'agent': 'generator',
            'status': f'Generating PoC ({i+1}/2)',
            'detail': f'Creating Foundry test for {vuln["vulnerability_type"]} vulnerability'
        })
        time.sleep(1)
        socketio.emit('agent_status', {
            'job_id': job_id,
            'agent': 'generator',
            'status': 'Code generated',
            'detail': f'Generated PoC file: PoC_{vuln["vulnerability_type"]}_{i+1}.t.sol'
        })
        socketio.emit('agent_complete', {
            'job_id': job_id,
            'agent': 'generator',
            'result': f'Created PoC_{vuln["vulnerability_type"]}_{i+1}.t.sol'
        })

        # Step 9: Runner
        time.sleep(1)
        socketio.emit('agent_active', {
            'job_id': job_id,
            'agent': 'runner',
            'status': f'Executing PoC ({i+1}/2)',
            'detail': f'Running Foundry test for {vuln["vulnerability_type"]}'
        })
        time.sleep(1)
        socketio.emit('agent_status', {
            'job_id': job_id,
            'agent': 'runner',
            'status': 'Execution complete',
            'detail': 'Test executed successfully on first attempt'
        })
        socketio.emit('agent_complete', {
            'job_id': job_id,
            'agent': 'runner',
            'result': 'Success'
        })

    # Step 10: Analysis Complete
    time.sleep(1)
    jobs[job_id]['status'] = 'completed'
    canned_results = {
        'rechecked_vulnerabilities': [
            {
                'vulnerability_type': 'Reentrancy',
                'skeptic_confidence': 0.95,
                'affected_functions': ['withdraw'],
                'reasoning': 'Function withdraw() allows reentrancy.',
                'validity_reasoning': 'Confirmed by skeptic agent.',
                'code_snippet': 'function withdraw() external { ... }'
            },
            {
                'vulnerability_type': 'Access Control',
                'skeptic_confidence': 0.92,
                'affected_functions': ['setAdmin'],
                'reasoning': 'setAdmin() is public and lacks onlyOwner.',
                'validity_reasoning': 'Confirmed by skeptic agent.',
                'code_snippet': 'function setAdmin(address) public { ... }'
            }
        ],
        'generated_pocs': [
            {
                'vulnerability': {'vulnerability_type': 'Reentrancy'},
                'exploit_plan': {
                    'setup_steps': ['Deploy attacker contract', 'Fund attacker'],
                    'execution_steps': ['Call withdraw()', 'Reenter withdraw()'],
                    'validation_steps': ['Check attacker balance increased']
                },
                'poc_data': {
                    'exploit_file': 'PoC_Reentrancy_1.t.sol',
                    'execution_command': 'forge test -vv --match-path "./src/test/PoC_Reentrancy_1.t.sol"',
                    'execution_results': {
                        'success': True,
                        'retries': 0,
                        'error': '',
                        'output': 'Test result: ok. 1 passed; 0 failed; 0 skipped; finished in 1.23s'
                    },
                    'exploit_code': '// SPDX-License-Identifier: UNLICENSED\npragma solidity ^0.8.15;\ncontract PoC_Reentrancy { /* ... */ }'
                }
            },
            {
                'vulnerability': {'vulnerability_type': 'Access Control'},
                'exploit_plan': {
                    'setup_steps': ['Deploy contract', 'Identify admin function'],
                    'execution_steps': ['Call setAdmin() as attacker', 'Change admin'],
                    'validation_steps': ['Check admin changed']
                },
                'poc_data': {
                    'exploit_file': 'PoC_Access_Control_2.t.sol',
                    'execution_command': 'forge test -vv --match-path "./src/test/PoC_Access_Control_2.t.sol"',
                    'execution_results': {
                        'success': True,
                        'retries': 0,
                        'error': '',
                        'output': 'Test result: ok. 1 passed; 0 failed; 0 skipped; finished in 1.10s'
                    },
                    'exploit_code': '// SPDX-License-Identifier: UNLICENSED\npragma solidity ^0.8.15;\ncontract PoC_Access_Control { /* ... */ }'
                }
            }
        ]
    }
    jobs[job_id]['results'] = canned_results
    socketio.emit('analysis_complete', {
        'job_id': job_id,
        'status': 'completed',
        'vulnerabilities_count': 2,
        'pocs_count': 2,
        'performance_metrics': {
            'total_lines': 120,
            'total_files': 3,
            'stages': [
                {'name': 'static_analysis', 'duration': 1.0},
                {'name': 'project_context_agent', 'duration': 1.0},
                {'name': 'analyzer_agent', 'duration': 1.0},
                {'name': 'skeptic_agent', 'duration': 1.0},
                {'name': 'exploiter_agent', 'duration': 2.0},
                {'name': 'generator_agent', 'duration': 2.0},
                {'name': 'exploit_runner', 'duration': 2.0},
                {'name': 'results_reporting', 'duration': 1.0}
            ]
        }
    })

@app.route('/api/demo-local-multicontract', methods=['POST'])
def start_demo_local_multicontract():
    """
    Starts a real analysis job using the local examples/MultiContract directory as the contracts_dir.
    """
    job_id = str(uuid.uuid4())
    contracts_dir = "/Users/advait/Desktop/NTU/fyp-fr/examples/MultiContract"
    # Use the first .sol file as the main contract_path for context
    main_file = None
    for fname in os.listdir(contracts_dir):
        if fname.endswith('.sol'):
            main_file = os.path.join(contracts_dir, fname)
            break
    if not main_file:
        return jsonify({'error': 'No .sol files found in MultiContract directory'}), 400
    jobs[job_id] = {
        'status': 'fetched',
        'contract_path': main_file,
        'contracts_dir': contracts_dir,
        'filename': os.path.basename(main_file),
        'save_separate': True
    }
    # Start analysis in a separate thread
    threading.Thread(
        target=analyze_thread,
        args=(job_id, main_file, ModelConfig(), {'auto_run': True, 'max_retries': 3}, True)
    ).start()
    return jsonify({'job_id': job_id, 'status': 'analyzing', 'message': 'Real local multi-file analysis started'})

# Custom SocketIO event handlers to provide real-time updates
@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

# Import the generator
from llm_agents.agents.generator import GeneratorAgent

# Modified version of GeneratorAgent for the frontend
class FrontendGeneratorAgent(GeneratorAgent):
    def generate(self, exploit_data):
        """Override parent generate method to use correct paths for frontend"""
        vuln_info = exploit_data.get("vulnerability", {})
        exploit_plan = exploit_data.get("exploit_plan", {})
        vuln_type = vuln_info.get("vulnerability_type", "unknown").replace(" ", "_")

        # Generate the PoC contract
        contract_code = self.generate_poc_contract(vuln_info, exploit_plan)

        # Save the contract to a file using our custom method - in the MAIN exploit directory
        filename = self.save_poc_locally(contract_code, vuln_type)

        # For the forge test command, we need to use just the relative path from within the exploit dir
        # Ensure filename is treated as an absolute path before calculating relative path
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_exploit_dir = os.path.join(project_root, "exploit")

        if os.path.isabs(filename):
             rel_path = os.path.relpath(filename, main_exploit_dir)
        else:
             # If filename is already relative (though save_poc_locally returns absolute)
             rel_path = filename

        # Make sure the relative path starts correctly for forge command
        if not rel_path.startswith('.'):
             rel_path = './' + rel_path

        return {
            "exploit_code": contract_code,
            "exploit_file": filename, # Return absolute path for reference
            "execution_command": f"forge test -vv --match-path \"{rel_path}\"" # Use quotes for paths with spaces
        }

    def save_poc_locally(self, poc_code: str, vuln_type: str) -> str:
        """
        Save the generated PoC contract to a file in the main exploit directory

        Args:
            poc_code: The Solidity code
            vuln_type: Type of vulnerability

        Returns:
            The absolute filename where the code was saved
        """
        # Use the main exploit directory instead of frontend_poc/exploit
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_exploit_dir = os.path.join(project_root, "exploit")
        test_dir = os.path.join(main_exploit_dir, "src", "test")
        os.makedirs(test_dir, exist_ok=True)

        ts = int(time.time())
        # Sanitize vuln_type for filename
        safe_vuln_type = re.sub(r'[^\w\-]+', '_', vuln_type)
        rel_filename = f"PoC_{safe_vuln_type}_{ts}.t.sol" # Standard forge test naming convention
        abs_filename = os.path.join(test_dir, rel_filename)

        # Extract solidity version from PoC code if possible, otherwise use default
        pragma_match = re.search(r"pragma solidity\s+([^\;]+);", poc_code)
        solidity_version = pragma_match.group(1).strip() if pragma_match else "^0.8.15" # Default version

        # Clean the PoC code: Remove markdown code blocks if present
        cleaned_code = poc_code
        if cleaned_code.strip().startswith("```solidity"):
            cleaned_code = cleaned_code.split("```solidity", 1)[1]
        if cleaned_code.strip().endswith("```"):
            cleaned_code = cleaned_code.rsplit("```", 1)[0]
        cleaned_code = cleaned_code.strip()

        # Ensure pragma solidity is at the top
        if not cleaned_code.startswith("pragma solidity"):
             # Find existing pragma and move it up, or add a new one
             existing_pragma_match = re.search(r"pragma solidity\s+([^\;]+);", cleaned_code)
             if existing_pragma_match:
                 pragma_line = existing_pragma_match.group(0)
                 cleaned_code = re.sub(r"pragma solidity\s+([^\;]+);", "", cleaned_code).strip()
                 cleaned_code = f"{pragma_line}\n{cleaned_code}"
             else:
                 cleaned_code = f"pragma solidity {solidity_version};\n{cleaned_code}"

        # Ensure SPDX license is present
        if not cleaned_code.startswith("// SPDX-License-Identifier:"):
             cleaned_code = f"// SPDX-License-Identifier: UNLICENSED\n{cleaned_code}"


        with open(abs_filename, "w", encoding="utf-8") as f:
            f.write(cleaned_code)

        print(f"PoC saved to {abs_filename}")
        return abs_filename

    def _generate_basetest_content(self) -> str:
        """Generates the content for basetest.sol"""
        # Ensure this content matches the expected basetest.sol content
        # Consider reading from a template file if it gets complex
        return """// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.15;

import "forge-std/Test.sol";

contract BaseTestWithBalanceLog is Test {
    // Change this to the target token to get token balance of
    // Keep it address(0) if its ETH that is gotten at the end of the exploit
    address fundingToken = address(0);

    struct ChainInfo {
        string name;
        string symbol;
    }

    mapping(uint256 => ChainInfo) private chainIdToInfo;

    constructor() {
        chainIdToInfo[1] = ChainInfo("MAINNET", "ETH");
        chainIdToInfo[238] = ChainInfo("BLAST", "ETH");
        chainIdToInfo[10] = ChainInfo("OPTIMISM", "ETH");
        chainIdToInfo[250] = ChainInfo("FANTOM", "FTM");
        chainIdToInfo[42_161] = ChainInfo("ARBITRUM", "ETH");
        chainIdToInfo[56] = ChainInfo("BSC", "BNB");
        chainIdToInfo[1285] = ChainInfo("MOONRIVER", "MOVR");
        chainIdToInfo[100] = ChainInfo("GNOSIS", "XDAI");
        chainIdToInfo[43_114] = ChainInfo("AVALANCHE", "AVAX");
        chainIdToInfo[137] = ChainInfo("POLYGON", "MATIC");
        chainIdToInfo[42_220] = ChainInfo("CELO", "CELO");
        chainIdToInfo[8453] = ChainInfo("BASE", "ETH");
    }

    function getChainInfo(
        uint256 chainId
    ) internal view returns (string memory, string memory) {
        ChainInfo storage info = chainIdToInfo[chainId];
        return (info.name, info.symbol);
    }

    function getChainSymbol(
        uint256 chainId
    ) internal view returns (string memory symbol) {
        (, symbol) = getChainInfo(chainId);
        // Return ETH as default if chainID is not registered in mapping
        if (bytes(symbol).length == 0) {
            symbol = "ETH";
        }
    }

    function getFundingBal() internal view returns (uint256) {
        return fundingToken == address(0)
            ? address(this).balance
            : TokenHelper.getTokenBalance(fundingToken, address(this));
    }

    function getFundingDecimals() internal view returns (uint8) {
        return fundingToken == address(0) ? 18 : TokenHelper.getTokenDecimals(fundingToken);
    }

    function getBaseCurrencySymbol() internal view returns (string memory) {
        string memory chainSymbol = getChainSymbol(block.chainid);
        return fundingToken == address(0) ? chainSymbol : TokenHelper.getTokenSymbol(fundingToken);
    }

    modifier balanceLog() {
        uint256 initialBalance = getFundingBal();
        logBalance("Before");

        // Ensure test contract has some initial ETH (needed for gas) if tracking ETH
        if (fundingToken == address(0) && address(this).balance < 1 ether) {
             vm.deal(address(this), address(this).balance + 10 ether); // Ensure at least 10 ETH for gas
        }

        _; // Execute the test function

        logBalance("After");

        // Optional assertion: Check if balance changed as expected (e.g., increased)
        // uint256 finalBalance = getFundingBal();
        // assertGt(finalBalance, initialBalance, "Attacker balance did not increase");
    }

    function logBalance(
        string memory stage
    ) internal {
        emit log_named_decimal_uint(
            string(abi.encodePacked("Attacker ", getBaseCurrencySymbol(), " Balance ", stage, " exploit")),
            getFundingBal(),
            getFundingDecimals()
        );
    }
}

library TokenHelper {
    function callTokenFunction(
        address tokenAddress,
        bytes memory data,
        bool staticCall
    ) private view returns (bytes memory) {
        (bool success, bytes memory result) = staticCall ? tokenAddress.staticcall(data) : tokenAddress.call(data);
        // Use view for staticcall to avoid state change errors if token isn't standard
        // For non-static calls, we can't guarantee success without reverts
        if (staticCall) {
             require(success, "Staticcall to token failed");
        } else if (!success) {
            // Try to decode revert reason for non-static calls
            if (result.length < 68) return bytes(""); // Not a standard revert string
            bytes memory revertReason = abi.decode(result[4:], (bytes));
            revert(string(revertReason));
        }
        return result;
    }

    function getTokenBalance(address tokenAddress, address targetAddress) internal view returns (uint256) {
        bytes memory result =
            callTokenFunction(tokenAddress, abi.encodeWithSignature("balanceOf(address)", targetAddress), true);
        return abi.decode(result, (uint256));
    }

    function getTokenDecimals(
        address tokenAddress
    ) internal view returns (uint8) {
        // Handle potential non-standard tokens
        (bool success, bytes memory result) = tokenAddress.staticcall(abi.encodeWithSignature("decimals()"));
        if (!success || result.length == 0) return 18; // Default to 18 if decimals() fails or is empty
        try abi.decode(result, (uint8)) returns (uint8 decimals) {
            return decimals;
        } catch {
            return 18; // Default if decoding fails
        }
    }

    function getTokenSymbol(
        address tokenAddress
    ) internal view returns (string memory) {
         // Handle potential non-standard tokens
        (bool success, bytes memory result) = tokenAddress.staticcall(abi.encodeWithSignature("symbol()"));
        if (!success || result.length == 0) return "TOKEN"; // Default if symbol() fails or is empty
        try abi.decode(result, (string memory)) returns (string memory symbol) {
            return symbol;
        } catch {
            return "TOKEN"; // Default if decoding fails
        }
    }
}

// Note: approveToken and transferToken removed as they cause state changes and aren't strictly needed for balance logging.
// If needed, they should be implemented in the main test contract, not the library.
"""


    def generate_basetest_file(self) -> str:
        """Generate basetest.sol and save it to the main exploit/src/test/basetest.sol"""
        # Use the main exploit directory
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_exploit_dir = os.path.join(project_root, "exploit")
        test_dir = os.path.join(main_exploit_dir, "src", "test")
        os.makedirs(test_dir, exist_ok=True) # Ensure directory exists
        abs_filename = os.path.join(test_dir, "basetest.sol")

        # Check if basetest.sol already exists
        if os.path.exists(abs_filename):
            print(f"Base test file already exists at {abs_filename}")
            return abs_filename

        # Generate the basetest file content
        basetest_content = self._generate_basetest_content()

        # Save the file
        with open(abs_filename, "w", encoding="utf-8") as f:
            f.write(basetest_content)

        print(f"Created base test file at {abs_filename}")
        return abs_filename

# Modified version of ExploitRunner for the frontend
class FrontendExploitRunner(ExploitRunner):
    def _execute_test(self, command: str) -> Tuple[bool, str, str]:
        """
        Execute a Foundry test command and capture the output.
        Uses the main exploit directory.

        Args:
            command: Forge test command to execute

        Returns:
            Tuple of (success, output, error_message)
        """
        try:
            # Use main exploit directory as the working directory
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            main_exploit_dir = os.path.join(project_root, "exploit")
            print(f"Executing test in directory: {main_exploit_dir}")
            print(f"Executing command: {command}") # Log the command being executed

            # Execute the command
            result = subprocess.run(
                command,
                shell=True, # shell=True is needed for complex commands with flags
                cwd=main_exploit_dir,
                capture_output=True,
                text=True,
                check=False # Don't raise exception on non-zero exit code
            )

            # Get complete output
            output = result.stdout + "\n" + result.stderr
            print(f"Command Output (stdout):\n{result.stdout}")
            if result.stderr:
                print(f"Command Output (stderr):\n{result.stderr}")


            # Check if the test passed based on forge output patterns
            success = False
            if "Test result: ok." in output and "0 failed" in output:
                 success = True
            # Handle case where specific test might pass but others fail
            elif "[PASS]" in result.stdout and "[FAIL]" not in result.stdout:
                 success = True

            if success:
                print("Test successful based on output.")
                return True, output, ""
            else:
                print("Test failed based on output.")
                 # Extract error message more reliably
                error_message = "Test failed. Check logs for details."
                # Look for common failure indicators
                fail_pattern = r"\[FAIL.*?\]|Error:|Reverted|Panic|CompilerError"
                fail_matches = re.findall(fail_pattern, output, re.IGNORECASE | re.DOTALL)
                if fail_matches:
                     # Prioritize compiler errors or explicit reverts
                     compiler_error = next((m for m in fail_matches if "CompilerError" in m), None)
                     revert_error = next((m for m in fail_matches if "Reverted" in m), None)
                     if compiler_error:
                          error_message = compiler_error
                     elif revert_error:
                          error_message = revert_error
                     else:
                          error_message = fail_matches[0] # First failure indicator found
                     error_message = error_message.strip() # Clean up
                # Fallback if specific patterns aren't found but command failed
                elif result.returncode != 0:
                     error_message = f"Forge command failed with exit code {result.returncode}. Error:\n{result.stderr[:500]}..." # Show stderr snippet

                print(f"Extracted error message: {error_message}")
                return False, output, error_message

        except Exception as e:
            print(f"Error during test execution: {str(e)}")
            return False, "", f"Error executing test: {str(e)}"

# Modified version of AgentCoordinator that emits SocketIO events
class SocketIOAgentCoordinator(AgentCoordinator):
    def __init__(self, job_id, model_config=None, use_rag=True):
        super().__init__(model_config, use_rag=use_rag)
        self.job_id = job_id
        # Replace the default runner and generator with our custom frontend versions
        self.runner = FrontendExploitRunner(model_config=model_config)
        self.generator = FrontendGeneratorAgent(model_config=model_config)
        # Use the global performance_tracker instance
        self.performance_tracker = performance_tracker

    def analyze_contract(self, contract_info, auto_run_config=None):
        # Set default auto-run config if none provided
        if auto_run_config is None:
            auto_run_config = {"auto_run": True, "max_retries": 3}

        # Configure runner's max retries
        self.runner.max_retries = auto_run_config.get("max_retries", 3)

        # 1. ProjectContextLLMAgent => inter-contract relationships
        if "contracts_dir" in contract_info and contract_info["contracts_dir"]:
            print(f"Emitting agent_active event for project_context_llm for job {self.job_id}")

            # Start performance tracking for project context agent
            if self.performance_tracker:
                 self.performance_tracker.start_stage("project_context_agent")

            socketio.emit('agent_active', {
                'job_id': self.job_id,
                'agent': 'project_context_llm',
                'status': 'Initializing inter-contract analysis',
                'detail': 'Starting LLM-powered analysis of contract relationships...'
            })

            # Emit status update before calling the ProjectContextLLMAgent
            socketio.emit('agent_status', {
                'job_id': self.job_id,
                'agent': 'project_context_llm',
                'status': 'Analyzing contract relationships',
                'detail': 'LLM is exploring contract relationships and dependencies'
            })

            # Run the project context analysis
            project_context_results = self.project_context.analyze_project(
                contract_info["contracts_dir"],
                contract_info.get("call_graph")
            )

            # Add project context to contract_info for the analyzer
            contract_info["project_context"] = project_context_results

            # Extract all insights from the enhanced project context structure
            insights = project_context_results.get("insights", [])
            dependencies = project_context_results.get("dependencies", [])
            vulnerabilities = project_context_results.get("vulnerabilities", [])
            recommendations = project_context_results.get("recommendations", [])
            important_functions = project_context_results.get("important_functions", [])
            stats = project_context_results.get("stats", {})
            contract_files_list = project_context_results.get("contract_files", []) # Changed name to avoid conflict

            # Prepare comprehensive insight details for the frontend
            insight_details = {
                'insights': insights[:15],  # General insights
                'dependencies': dependencies[:15],  # Contract dependencies and relationships
                'vulnerabilities': vulnerabilities[:15],  # Potential vulnerabilities
                'recommendations': recommendations[:15],  # Security recommendations
                'important_functions': important_functions[:15],  # Important cross-contract functions
                'contract_files': [os.path.basename(f) for f in contract_files_list[:10]], # Show base names
                'stats': {
                    'total_contracts': stats.get('total_contracts', 0),
                    'total_relationships': stats.get('total_relationships', 0),
                    'total_vulnerabilities': stats.get('total_vulnerabilities', 0),
                    'total_recommendations': stats.get('total_recommendations', 0)
                }
            }

            # Emit comprehensive insights to frontend
            socketio.emit('project_context_insights', {
                'job_id': self.job_id,
                'details': insight_details
            })

            # Emit status updates for each type of finding
            if vulnerabilities:
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'project_context_llm',
                    'status': 'Identified potential vulnerabilities',
                    'detail': f'Found {len(vulnerabilities)} potential security issues in contract interactions'
                })

            if important_functions:
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'project_context_llm',
                    'status': 'Identified key functions',
                    'detail': f'Found {len(important_functions)} important cross-contract functions'
                })

            # Mark the project context agent as complete with a more comprehensive summary
            summary_parts = []
            if insights: summary_parts.append(f'{len(insights)} insights')
            if dependencies: summary_parts.append(f'{len(dependencies)} dependencies')
            if vulnerabilities: summary_parts.append(f'{len(vulnerabilities)} potential vulnerabilities')
            if recommendations: summary_parts.append(f'{len(recommendations)} recommendations')

            summary = f'Analyzed {stats.get("total_contracts", 0)} contracts. Found: {", ".join(summary_parts)}' if summary_parts else f'Analyzed {stats.get("total_contracts", 0)} contracts.'


            socketio.emit('agent_complete', {
                'job_id': self.job_id,
                'agent': 'project_context_llm',
                'result': summary
            })

            # End performance tracking for project context agent
            if self.performance_tracker:
                 self.performance_tracker.end_stage()


        # 2. Analyzer => all vulnerabilities
        print(f"Emitting agent_active event for analyzer for job {self.job_id}")

        # Start performance tracking for analyzer agent
        if self.performance_tracker:
            self.performance_tracker.start_stage("analyzer_agent")

        socketio.emit('agent_active', {
            'job_id': self.job_id,
            'agent': 'analyzer',
            'status': 'Initializing vulnerability analysis',
            'detail': 'Loading contract code and preparing for analysis...'
        })

        # Emit status update before calling analyzer
        socketio.emit('agent_status', {
            'job_id': self.job_id,
            'agent': 'analyzer',
            'status': 'Analyzing contract code',
            'detail': f'Searching for potential vulnerabilities. Using RAG: {self.use_rag}'
        })

        # We need to capture the RAG details to emit them
        if self.use_rag and self.analyzer.retriever: # Check if retriever exists
            # Emit status update about RAG process starting
            socketio.emit('agent_status', {
                'job_id': self.job_id,
                'agent': 'analyzer',
                'status': 'Retrieving similar vulnerabilities',
                'detail': 'Searching knowledge base for relevant vulnerability patterns'
            })

            try:
                # Get the query text and retrieve relevant docs manually
                # Use source_code from contract_info, which should be the flattened code
                query_text = contract_info.get("source_code", "")
                if not query_text:
                    print("Warning: No source code found for RAG query.")
                    relevant_docs = []
                else:
                    relevant_docs = self.analyzer.retriever.invoke(query_text)

                # Prepare RAG details to emit
                rag_details = []
                for i, doc in enumerate(relevant_docs, start=1):
                    meta = doc.metadata
                    filename = meta.get('filename', 'Unknown')
                    # Ensure lines_range handles potential non-numeric values gracefully
                    start_line = meta.get('start_line', '?')
                    end_line = meta.get('end_line', '?')
                    lines_range = f"{start_line}-{end_line}"
                    vuln_categories = meta.get("vuln_categories", [])
                    content_preview = doc.page_content[:500] + "..." if len(doc.page_content) > 500 else doc.page_content

                    rag_details.append({
                        'filename': filename,
                        'lines_range': lines_range,
                        'vuln_categories': vuln_categories,
                        'content_preview': content_preview
                    })

                # Emit RAG details
                socketio.emit('rag_details', {
                    'job_id': self.job_id,
                    'details': rag_details
                })

                # Emit status update about RAG results
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'analyzer',
                    'status': 'Retrieved similar vulnerabilities',
                    'detail': f'Found {len(relevant_docs)} similar vulnerability patterns to guide analysis'
                })
            except Exception as rag_error:
                print(f"Error during RAG retrieval: {rag_error}")
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'analyzer',
                    'status': 'RAG Retrieval Failed',
                    'detail': f'Error: {str(rag_error)}'
                })

        # Run the analyzer
        vuln_results = self.analyzer.analyze(contract_info)
        vulnerabilities = vuln_results.get("vulnerabilities", [])

        # End performance tracking for analyzer agent
        if self.performance_tracker:
            self.performance_tracker.end_stage()

        # Emit status with results summary
        socketio.emit('agent_status', {
            'job_id': self.job_id,
            'agent': 'analyzer',
            'status': 'Analysis complete',
            'detail': f'Found {len(vulnerabilities)} potential vulnerabilities to investigate'
        })

        print(f"Emitting agent_complete event for analyzer for job {self.job_id}")
        socketio.emit('agent_complete', {
            'job_id': self.job_id,
            'agent': 'analyzer',
            'result': f'Found {len(vulnerabilities)} potential vulnerabilities'
        })

        if not vulnerabilities:
             print("No vulnerabilities found by analyzer.")
             # Ensure performance tracking stops correctly
             if self.performance_tracker and self.performance_tracker.current_stage is not None:
                  self.performance_tracker.end_stage()
             return {"status": "no_vulnerability_found", "rechecked_vulnerabilities": [], "generated_pocs": []}


        # 2. Skeptic => re-check validity
        print(f"Emitting agent_active event for skeptic for job {self.job_id}")

        # Start performance tracking for skeptic agent
        if self.performance_tracker:
            self.performance_tracker.start_stage("skeptic_agent")

        socketio.emit('agent_active', {
            'job_id': self.job_id,
            'agent': 'skeptic',
            'status': 'Initializing validity check',
            'detail': 'Preparing to verify the reported vulnerabilities...'
        })

        # Emit status update with number of vulnerabilities to check
        socketio.emit('agent_status', {
            'job_id': self.job_id,
            'agent': 'skeptic',
            'status': 'Validating findings',
            'detail': f'Checking validity of {len(vulnerabilities)} potential vulnerabilities'
        })

        # Run the skeptic
        rechecked_vulns = self.skeptic.audit_vulnerabilities(
            contract_info["source_code"], vulnerabilities
        )

        # End performance tracking for skeptic agent
        if self.performance_tracker:
            self.performance_tracker.end_stage()

        # Count high confidence vulnerabilities - with float conversion for safety
        high_conf_count = len([v for v in rechecked_vulns if float(v.get("skeptic_confidence", 0)) > 0.5])

        # Emit status with results summary
        socketio.emit('agent_status', {
            'job_id': self.job_id,
            'agent': 'skeptic',
            'status': 'Validation complete',
            'detail': f'Found {high_conf_count} high-confidence vulnerabilities'
        })

        print(f"Emitting agent_complete event for skeptic for job {self.job_id}")
        socketio.emit('agent_complete', {
            'job_id': self.job_id,
            'agent': 'skeptic',
            'result': f'Confirmed {high_conf_count} vulnerabilities with high confidence'
        })

        # 3. Generate PoCs for high-confidence vulnerabilities
        generated_pocs = []
        high_conf_vulns = [v for v in rechecked_vulns if float(v.get("skeptic_confidence", 0)) > 0.5]

        if high_conf_vulns:
            for i, vul in enumerate(high_conf_vulns):
                # EXPLOITER AGENT
                vuln_type = vul.get('vulnerability_type', 'Unknown')
                print(f"Emitting agent_active event for exploiter for job {self.job_id}")

                # Start performance tracking for exploiter agent
                if self.performance_tracker:
                     self.performance_tracker.start_stage("exploiter_agent")

                socketio.emit('agent_active', {
                    'job_id': self.job_id,
                    'agent': 'exploiter',
                    'status': f'Creating exploit plan ({i+1}/{len(high_conf_vulns)})',
                    'detail': f'Planning attack strategy for {vuln_type} vulnerability'
                })

                # Emit status update with vulnerability details
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'exploiter',
                    'status': 'Analyzing vulnerability',
                    'detail': f'Determining required steps to demonstrate {vuln_type}'
                })

                # Run the exploiter
                plan_data = self.exploiter.generate_exploit_plan(vul)
                # Include vulnerability info in plan_data for generator
                plan_data["vulnerability"] = vul

                # End performance tracking for exploiter agent
                if self.performance_tracker:
                     self.performance_tracker.end_stage()

                # Get step counts for details
                setup_steps = len(plan_data.get('exploit_plan', {}).get('setup_steps', []))
                exec_steps = len(plan_data.get('exploit_plan', {}).get('execution_steps', []))
                valid_steps = len(plan_data.get('exploit_plan', {}).get('validation_steps', []))

                # Emit status with plan summary
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'exploiter',
                    'status': 'Plan created',
                    'detail': f'Exploit plan has {setup_steps} setup, {exec_steps} execution, and {valid_steps} validation steps'
                })

                print(f"Emitting agent_complete event for exploiter for job {self.job_id}")
                socketio.emit('agent_complete', {
                    'job_id': self.job_id,
                    'agent': 'exploiter',
                    'result': f'Created exploit plan for {vuln_type}'
                })

                # Skip PoC generation if configured
                if self.model_config.skip_poc_generation:
                    print(f"Skipping PoC generation for job {self.job_id} as requested")
                    # Store just the exploit plan
                    generated_pocs.append({
                        "vulnerability": vul,
                        "exploit_plan": plan_data.get("exploit_plan"),
                    })
                    continue

                # GENERATOR AGENT
                print(f"Emitting agent_active event for generator for job {self.job_id}")

                # Start performance tracking for generator agent
                if self.performance_tracker:
                     self.performance_tracker.start_stage("generator_agent")

                socketio.emit('agent_active', {
                    'job_id': self.job_id,
                    'agent': 'generator',
                    'status': f'Generating PoC ({i+1}/{len(high_conf_vulns)})',
                    'detail': f'Creating Foundry test for {vuln_type} vulnerability'
                })

                # Emit status for generating the basetest.sol file
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'generator',
                    'status': 'Creating helper files',
                    'detail': 'Ensuring required base test files are available'
                })

                # The basetest.sol file is automatically created by the FrontendGeneratorAgent
                self.generator.generate_basetest_file() # Ensure it's called

                # Emit status for code generation
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'generator',
                    'status': 'Writing exploit code',
                    'detail': 'Generating Solidity code to demonstrate the vulnerability'
                })

                # Generate the PoC for this vulnerability
                poc_data = self.generator.generate(plan_data)
                filename = os.path.basename(poc_data.get('exploit_file', 'Unknown'))

                # End performance tracking for generator agent
                if self.performance_tracker:
                    self.performance_tracker.end_stage()

                # Emit status with file details
                socketio.emit('agent_status', {
                    'job_id': self.job_id,
                    'agent': 'generator',
                    'status': 'Code generated',
                    'detail': f'Generated PoC file: {filename}'
                })

                print(f"Emitting agent_complete event for generator for job {self.job_id}")
                socketio.emit('agent_complete', {
                    'job_id': self.job_id,
                    'agent': 'generator',
                    'result': f'Created {filename}'
                })

                # Run and fix the exploit if auto-run is enabled
                if auto_run_config.get("auto_run", True):
                    # RUNNER AGENT
                    print(f"Emitting agent_active event for runner for job {self.job_id}")

                    # Start performance tracking for runner agent
                    if self.performance_tracker:
                         self.performance_tracker.start_stage("exploit_runner")

                    socketio.emit('agent_active', {
                        'job_id': self.job_id,
                        'agent': 'runner',
                        'status': f'Executing PoC ({i+1}/{len(high_conf_vulns)})',
                        'detail': f'Running Foundry test for {vuln_type}'
                    })

                    # Emit status for executing test
                    socketio.emit('agent_status', {
                        'job_id': self.job_id,
                        'agent': 'runner',
                        'status': 'Running test',
                        'detail': f'Executing: {poc_data.get("execution_command", "forge test")}'
                    })

                    # Run the test
                    run_result = self.runner.run_and_fix_exploit(poc_data)

                    # End performance tracking for runner agent
                    if self.performance_tracker:
                         self.performance_tracker.end_stage()


                    # Get detailed results for feedback
                    success = run_result.get('success', False)
                    retries = run_result.get('retries', 0)

                    # Create appropriate status based on result
                    if success:
                        status_detail = f'Test executed successfully on first attempt' if retries == 0 else f'Test fixed and executed successfully after {retries} retries'
                    else:
                        status_detail = f'Test failed after {retries} fix attempts'

                    # Emit status with execution results
                    socketio.emit('agent_status', {
                        'job_id': self.job_id,
                        'agent': 'runner',
                        'status': 'Execution complete',
                        'detail': status_detail
                    })

                    print(f"Emitting agent_complete event for runner for job {self.job_id}")
                    socketio.emit('agent_complete', {
                        'job_id': self.job_id,
                        'agent': 'runner',
                        'result': 'Success' if success else f'Failed after {retries} attempts'
                    })

                    # Add execution results to the PoC data
                    poc_data["execution_results"] = {
                        "success": run_result.get("success", False),
                        "retries": run_result.get("retries", 0),
                        "error": run_result.get("error", ""),
                        "output": run_result.get("output", "")[:1000]  # Truncate long outputs
                    }
                else:
                     # Add placeholder if auto-run is off
                     poc_data["execution_results"] = { "success": None, "retries": 0, "error": "Auto-run disabled", "output": "" }


                generated_pocs.append({
                    "vulnerability": vul,
                    "exploit_plan": plan_data.get("exploit_plan"),
                    "poc_data": poc_data,
                })

        # End any potentially running agent stage before final reporting
        if self.performance_tracker and self.performance_tracker.current_stage is not None:
            self.performance_tracker.end_stage()

        # Start performance tracking for results reporting/export
        if self.performance_tracker:
            self.performance_tracker.start_stage("results_reporting")

        # Prepare the final result object
        result = {
            "rechecked_vulnerabilities": rechecked_vulns,
            "generated_pocs": generated_pocs,
        }

        # Export report as markdown if configured
        if self.model_config.export_markdown:
            self.export_results_to_markdown(contract_info["source_code"], result)

        # End the final stage
        if self.performance_tracker:
            self.performance_tracker.end_stage()

        return result

    def export_results_to_markdown(self, contract_code, results):
        """Export analysis results to a markdown file in the uploads directory"""
        from datetime import datetime
        import os

        # Create output filename based on the job id
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Use the UPLOAD_FOLDER defined globally in app.py
        output_dir = UPLOAD_FOLDER
        os.makedirs(output_dir, exist_ok=True)
        # Sanitize job_id if needed
        safe_job_id = re.sub(r'[^\w\-]+', '_', self.job_id)
        output_file = os.path.join(output_dir, f"analysis_report_{safe_job_id}_{timestamp}.md")


        print(f"Exporting analysis report to {output_file}")

        rechecked_vulns = results.get("rechecked_vulnerabilities", [])
        pocs = results.get("generated_pocs", [])

        try:
            with open(output_file, "w", encoding='utf-8') as f: # Specify encoding
                # Write header
                f.write(f"# Smart Contract Vulnerability Analysis Report\n\n")
                f.write(f"**Job ID:** {self.job_id}\n")
                f.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

                # Add a code preview (first few lines)
                code_preview = "\n".join(contract_code.split("\n")[:20]) + "\n..."
                f.write(f"**Contract Preview:**\n\n```solidity\n{code_preview}\n```\n\n")

                # Vulnerability summary
                f.write(f"## Vulnerability Summary\n\n")
                if not rechecked_vulns:
                    f.write("No vulnerabilities were detected in this contract.\n\n")
                else:
                    f.write(f"Found {len(rechecked_vulns)} potential vulnerabilities:\n\n")

                    # Create a summary table
                    f.write("| # | Vulnerability Type | Confidence | Affected Functions |\n")
                    f.write("|---|-------------------|------------|--------------------|\n")

                    for idx, vuln in enumerate(rechecked_vulns, start=1):
                        vuln_type = vuln.get('vulnerability_type', 'Unknown')
                        # Ensure confidence is float before formatting
                        try:
                            confidence = float(vuln.get('skeptic_confidence', 0))
                        except (ValueError, TypeError):
                            confidence = 0.0
                        affected = ', '.join(vuln.get('affected_functions', ['Unknown']))
                        f.write(f"| {idx} | {vuln_type} | {confidence:.2f} | {affected} |\n")

                    f.write("\n")

                # Detailed vulnerability analysis
                if rechecked_vulns:
                    f.write("## Detailed Analysis\n\n")

                    for idx, vuln in enumerate(rechecked_vulns, start=1):
                        vuln_type = vuln.get('vulnerability_type', 'Unknown')
                        try:
                             confidence = float(vuln.get('skeptic_confidence', 0))
                        except (ValueError, TypeError):
                             confidence = 0.0


                        f.write(f"### Vulnerability #{idx}: {vuln_type}\n\n")
                        f.write(f"**Confidence:** {confidence:.2f}\n\n")

                        if vuln.get('reasoning'):
                            f.write(f"**Reasoning:**\n\n```\n{vuln.get('reasoning')}\n```\n\n") # Use code block for reasoning

                        if vuln.get('validity_reasoning'):
                            f.write(f"**Validation:**\n\n```\n{vuln.get('validity_reasoning')}\n```\n\n") # Use code block

                        if vuln.get('code_snippet'):
                            f.write(f"**Code Snippet:**\n\n```solidity\n{vuln.get('code_snippet')}\n```\n\n")

                        if vuln.get('affected_functions'):
                            f.write(f"**Affected Functions:** {', '.join(vuln.get('affected_functions'))}\n\n")

                        # Look for a corresponding PoC
                        matching_poc = next((p for p in pocs if p.get("vulnerability", {}).get("vulnerability_type") == vuln_type), None)
                        if matching_poc and matching_poc.get("exploit_plan"):
                            f.write("**Exploit Plan:**\n\n")

                            # Add all steps from the exploit plan
                            plan = matching_poc["exploit_plan"]

                            if plan.get("setup_steps"):
                                f.write("*Setup Steps:*\n\n")
                                for step in plan.get("setup_steps", []):
                                    f.write(f"- {step}\n")
                                f.write("\n")

                            if plan.get("execution_steps"):
                                f.write("*Execution Steps:*\n\n")
                                for step in plan.get("execution_steps", []):
                                    f.write(f"- {step}\n")
                                f.write("\n")

                            if plan.get("validation_steps"):
                                f.write("*Validation Steps:*\n\n")
                                for step in plan.get("validation_steps", []):
                                    f.write(f"- {step}\n")
                                f.write("\n")

                        # Add a separator between vulnerabilities
                        f.write("---\n\n")

                # PoC information if any were generated and not skipped
                if pocs and any("poc_data" in poc for poc in pocs):
                    f.write("## Proof of Concept Exploits\n\n")

                    for idx, poc in enumerate(pocs, start=1):
                        if "poc_data" not in poc:
                            continue

                        vuln = poc['vulnerability']
                        vuln_type = vuln.get('vulnerability_type', 'Unknown')

                        f.write(f"### PoC #{idx}: {vuln_type}\n\n")

                        poc_data = poc["poc_data"]
                        exploit_file = poc_data.get('exploit_file', 'N/A')
                        f.write(f"**File:** `{os.path.basename(exploit_file)}`\n\n") # Show only basename

                        # Add execution results if available
                        if "execution_results" in poc_data:
                            results_data = poc_data["execution_results"] # Renamed variable
                            if results_data.get("success"):
                                f.write("**Execution:** ✅ SUCCESS\n\n")
                            elif results_data.get("success") is False: # Explicitly check for False
                                f.write(f"**Execution:** ❌ FAILED after {results_data.get('retries', 0)} fix attempts\n\n")

                                if results_data.get("error"):
                                    f.write(f"**Error:**\n```\n{results_data.get('error')}\n```\n\n")
                            else: # Handle case where success is None (e.g., auto-run disabled)
                                 f.write(f"**Execution:** ❔ SKIPPED (Auto-run disabled)\n\n")

                        # Add PoC code if available
                        if poc_data.get("exploit_code"):
                            f.write("**Exploit Code:**\n\n```solidity\n")
                            f.write(poc_data.get("exploit_code"))
                            f.write("\n```\n\n")

                        # Add a separator between PoCs
                        f.write("---\n\n")

                # Footer with recommendations
                f.write("## Recommendations\n\n")
                f.write("For each identified vulnerability, consider implementing the following mitigations:\n\n")

                # Add generic recommendations based on found vulnerability types
                found_vuln_types = {v.get('vulnerability_type', '').lower() for v in rechecked_vulns}

                if any('reentrancy' in vt for vt in found_vuln_types):
                    f.write("- **For Reentrancy**: Implement checks-effects-interactions pattern and consider using OpenZeppelin's ReentrancyGuard.\n")

                if any(term in vt for vt in found_vuln_types for term in ['overflow', 'underflow', 'arithmetic']):
                    f.write("- **For Arithmetic Issues**: Use Solidity 0.8.x+ for built-in overflow/underflow checks or use OpenZeppelin's SafeMath library for older versions.\n")

                if any(term in vt for vt in found_vuln_types for term in ['access', 'authorization', 'permission']):
                    f.write("- **For Access Control**: Implement proper authorization checks (e.g., `onlyOwner`, role-based access control) and verify access control logic thoroughly.\n")

                if any(term in vt for vt in found_vuln_types for term in ['oracle', 'price']):
                    f.write("- **For Oracle Manipulation**: Use time-weighted average prices (TWAP) and consider using multiple independent oracle sources (e.g., Chainlink Price Feeds).\n")

                if any('unchecked' in vt for vt in found_vuln_types):
                     f.write("- **For Unchecked Calls/Returns**: Always check the return value of low-level calls and external contract calls. Use `call` with caution.\n")

                # Add a general recommendation
                f.write("- **For All Vulnerabilities**: Conduct thorough testing, including unit tests, integration tests, and fuzzing. Consider a professional security audit before deploying to mainnet.\n\n")

                f.write("*This report was generated automatically by the Smart Contract Vulnerability Analyzer.*\n")

        except Exception as e:
            print(f"Error writing markdown report to {output_file}: {e}")
            # Don't emit if writing failed
            return

        # Also emit an event to notify the frontend
        socketio.emit('report_exported', {
            'job_id': self.job_id,
            'report_path': os.path.basename(output_file), # Send only basename
            'filename': os.path.basename(output_file)
        })

if __name__ == '__main__':
    # Match the port with your socket in the frontend
    socketio.run(app, debug=True, host='0.0.0.0', port=3000, allow_unsafe_werkzeug=True)
