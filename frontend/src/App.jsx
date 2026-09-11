import React, { useState, useEffect, useRef } from 'react';
import {
  FileText,
  Upload,
  Download,
  Copy,
  Check,
  RefreshCw,
  AlertCircle,
  Folder,
  Settings,
  Layers,
  Sparkles,
  ChevronDown,
  ChevronUp,
  History,
  X
} from 'lucide-react';
import HistoryView from './HistoryView';

export default function App() {
  const [view, setView] = useState(window.location.hash === '#history' ? 'history' : 'convert');
  // Input state
  const [activeTab, setActiveTab] = useState('upload'); // 'upload' or 'paste'
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [pastedText, setPastedText] = useState('');
  const [isDragging, setIsDragging] = useState(false);

  // Configuration state
  const [actions, setActions] = useState(['create_flashcards']);
  const [connector, setConnector] = useState('openai');
  const [model, setModel] = useState('');
  const [outputFormat, setOutputFormat] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [strategy, setStrategy] = useState('');
  const [chunkTokens, setChunkTokens] = useState('');
  const [temperature, setTemperature] = useState('');

  // Backend metadata & status
  const [configMeta, setConfigMeta] = useState(null);
  const [backendStatus, setBackendStatus] = useState('checking'); // 'online', 'offline', 'checking'

  // Execution & results state
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [results, setResults] = useState([]);
  const [copiedId, setCopiedId] = useState(null);
  const [batchProgress, setBatchProgress] = useState({ completed: 0, total: 0 });

  const fileInputRef = useRef(null);

  // Fetch backend configuration on mount
  useEffect(() => {
    fetchConfig();
  }, []);

  useEffect(() => {
    const handleHashChange = () => setView(window.location.hash === '#history' ? 'history' : 'convert');
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  const navigate = (nextView) => {
    window.location.hash = nextView === 'history' ? 'history' : '';
    setView(nextView);
  };

  // Listen for global clipboard paste events
  useEffect(() => {
    const handlePaste = (e) => {
      if (e.clipboardData && e.clipboardData.files.length > 0) {
        appendFiles(Array.from(e.clipboardData.files));
        setActiveTab('upload');
      }
    };
    window.addEventListener('paste', handlePaste);
    return () => window.removeEventListener('paste', handlePaste);
  }, []);

  const fetchConfig = async () => {
    try {
      const res = await fetch('/api/config');
      if (!res.ok) throw new Error('Backend responded with error');
      const data = await res.json();
      setConfigMeta(data);
      setBackendStatus('online');

      // Initialize default model if connector default exists
      if (data.connectors && data.connectors[connector]) {
        setModel(data.connectors[connector].default_model || '');
      }
    } catch (err) {
      console.warn('Backend connection error:', err);
      setBackendStatus('offline');
    }
  };

  const handleConnectorChange = (newConnector) => {
    setConnector(newConnector);
    if (configMeta?.connectors?.[newConnector]) {
      setModel(configMeta.connectors[newConnector].default_model || '');
    }
  };

  const handleActionChange = (newAction) => {
    setActions((current) => (
      current.includes(newAction)
        ? current.filter((item) => item !== newAction)
        : [...current, newAction]
    ));
  };

  const appendFiles = (incomingFiles) => {
    setSelectedFiles((current) => {
      const next = [...current];
      incomingFiles.forEach((file) => {
        const duplicate = next.some((item) => (
          item.name === file.name && item.size === file.size && item.lastModified === file.lastModified
        ));
        if (!duplicate) next.push(file);
      });
      return next;
    });
  };

  // Drag and drop handlers
  const handleDragOver = (e) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      appendFiles(Array.from(e.dataTransfer.files));
    }
  };

  const handleFileChange = (e) => {
    if (e.target.files && e.target.files.length > 0) {
      appendFiles(Array.from(e.target.files));
      e.target.value = '';
    }
  };

  const clearSelectedFile = (fileToRemove) => {
    setSelectedFiles((current) => current.filter((file) => file !== fileToRemove));
  };

  // Convert submission
  const handleConvert = async (e) => {
    e.preventDefault();
    setError(null);
    setResults([]);

    if (activeTab === 'upload' && selectedFiles.length === 0) {
      setError('Please select or drop one or more PDF or document files to convert.');
      return;
    }
    if (activeTab === 'paste' && !pastedText.trim()) {
      setError('Please paste document content to convert.');
      return;
    }
    if (actions.length === 0) {
      setError('Please select at least one learning artifact.');
      return;
    }

    setLoading(true);
    const groupId = () => crypto.randomUUID().replaceAll('-', '');
    const sources = activeTab === 'upload'
      ? selectedFiles.map((file) => ({ file, label: file.name, groupId: groupId() }))
      : [{ text: pastedText, label: 'pasted_document.txt', groupId: groupId() }];
    const jobs = sources.flatMap((source) => actions.map((selectedAction) => ({ source, action: selectedAction })));
    const failures = [];
    let nextJob = 0;
    setBatchProgress({ completed: 0, total: jobs.length });

    const runJob = async (job, index) => {
      const formData = new FormData();
      formData.append('action', job.action);
      formData.append('connector', connector);
      formData.append('enqueue', 'true');
      formData.append('source_group_id', job.source.groupId);
      if (model.trim()) formData.append('model', model.trim());
      if (outputFormat.trim()) formData.append('output_format', outputFormat.trim());
      if (apiKey.trim()) formData.append('api_key', apiKey.trim());
      if (strategy) formData.append('strategy', strategy);
      if (chunkTokens) formData.append('chunk_tokens', chunkTokens);
      if (temperature) formData.append('temperature', temperature);
      if (job.source.file) {
        formData.append('file', job.source.file);
      } else {
        formData.append('pasted_text', job.source.text);
        formData.append('input_filename', job.source.label);
      }

      const response = await fetch('/api/convert', { method: 'POST', body: formData });
      let data = {};
      try {
        data = await response.json();
      } catch {
        // The status text below remains useful if a proxy returns a non-JSON response.
      }
      if (!response.ok) {
        throw new Error(`${job.source.label} · ${configMeta?.actions?.[job.action]?.title || job.action}: ${data.detail || response.statusText || 'Conversion failed.'}`);
      }
      if (!data.queued) {
        setResults((current) => [...current, { ...data, jobIndex: index }].sort((a, b) => a.jobIndex - b.jobIndex));
      }
    };

    const worker = async () => {
      while (nextJob < jobs.length) {
        const index = nextJob;
        nextJob += 1;
        try {
          await runJob(jobs[index], index);
        } catch (err) {
          failures.push(err.message || 'An unexpected conversion error occurred.');
        } finally {
          setBatchProgress((current) => ({ ...current, completed: current.completed + 1 }));
        }
      }
    };

    await Promise.all(Array.from({ length: Math.min(3, jobs.length) }, () => worker()));
    if (failures.length > 0) {
      setError(`${failures.length} of ${jobs.length} conversions failed. ${failures[0]}`);
    } else {
      navigate('history');
    }
    setLoading(false);
  };

  const handleCopy = (result) => {
    if (!result.output_text) return;
    navigator.clipboard.writeText(result.output_text);
    setCopiedId(result.session_id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const sourceCount = activeTab === 'upload' ? selectedFiles.length : (pastedText.trim() ? 1 : 0);
  const plannedConversions = sourceCount * actions.length;

  return (
    <div className="container">
      {/* Header */}
      <header className="app-header">
        <div>
          <div className="header-title-row">
            <Sparkles className="dropzone-icon" style={{ width: 28, height: 28, color: '#3b82f6', margin: 0 }} />
            <h1 className="app-title">Content Generator Studio</h1>
          </div>
          <p className="app-subtitle">
            Transform PDFs and documents into structured study artifacts, data tables, quizzes, and mind maps
          </p>
        </div>
        <div className="header-actions">
          <button
            type="button"
            className={`nav-btn ${view === 'convert' ? 'active' : ''}`}
            onClick={() => navigate('convert')}
          >
            <Sparkles size={16} /> Convert
          </button>
          <button
            type="button"
            className={`nav-btn ${view === 'history' ? 'active' : ''}`}
            onClick={() => navigate('history')}
          >
            <History size={16} /> History
          </button>
          <span className={`backend-badge ${backendStatus === 'offline' ? 'offline' : ''}`}>
            <span className="dot"></span>
            {backendStatus === 'online' ? 'Backend Ready' : backendStatus === 'offline' ? 'Backend Offline' : 'Connecting...'}
          </span>
        </div>
      </header>

      {view === 'history' ? <HistoryView /> : (
        <>

      {/* Error banner */}
      {error && (
        <div className="error-banner">
          <AlertCircle style={{ flexShrink: 0, marginTop: 2 }} size={20} />
          <div>
            <strong>Conversion Error:</strong> {error}
          </div>
        </div>
      )}

      {/* Main 2-column layout */}
      <div className="main-grid">
        {/* Left Column: Input & Configuration */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
          {/* Card 1: Document Input */}
          <div className="card">
            <h2 className="card-title">
              <FileText size={20} color="#3b82f6" /> 1. Source Document
            </h2>

            {/* Input tabs */}
            <div className="tab-row">
              <button
                type="button"
                className={`tab-btn ${activeTab === 'upload' ? 'active' : ''}`}
                onClick={() => setActiveTab('upload')}
              >
                <Upload size={16} /> Select / Drop Files
              </button>
              <button
                type="button"
                className={`tab-btn ${activeTab === 'paste' ? 'active' : ''}`}
                onClick={() => setActiveTab('paste')}
              >
                <Copy size={16} /> Paste Content
              </button>
            </div>

            {activeTab === 'upload' ? (
              <div>
                <div
                  className={`dropzone ${isDragging ? 'dragover' : ''}`}
                  onDragOver={handleDragOver}
                  onDragLeave={handleDragLeave}
                  onDrop={handleDrop}
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Upload className="dropzone-icon" />
                  <p className="dropzone-text">Click to choose files or drag & drop them here</p>
                  <p className="dropzone-hint">
                    Supports PDF (.pdf), Word (.docx), Markdown (.md), Text (.txt), JSON, LaTeX
                  </p>
                  <div className="dropzone-paste-badge">
                    Tip: You can also paste copied PDF files with Ctrl+V / Cmd+V
                  </div>
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    accept=".pdf,.docx,.txt,.md,.markdown,.tex,.rst,.json,.csv,.html,.htm"
                    onChange={handleFileChange}
                    style={{ display: 'none' }}
                  />
                </div>

                {selectedFiles.length > 0 && (
                  <div className="selected-files">
                    <div className="selection-summary">
                      <span>{selectedFiles.length} file{selectedFiles.length === 1 ? '' : 's'} selected</span>
                      <button type="button" className="text-btn" onClick={() => setSelectedFiles([])}>Clear all</button>
                    </div>
                    {selectedFiles.map((selectedFile) => (
                      <div className="file-badge" key={`${selectedFile.name}-${selectedFile.size}-${selectedFile.lastModified}`}>
                        <div className="file-badge-left">
                          <FileText size={24} color="#3b82f6" />
                          <div>
                            <div className="file-name">{selectedFile.name}</div>
                            <div className="file-size">{(selectedFile.size / 1024).toFixed(1)} KB</div>
                          </div>
                        </div>
                        <button type="button" className="remove-btn" onClick={() => clearSelectedFile(selectedFile)} title="Remove file">
                          <X size={18} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div>
                <label className="form-label">Paste Raw Document Text / Markdown</label>
                <textarea
                  className="form-textarea"
                  placeholder="Paste text content or book chapter here..."
                  value={pastedText}
                  onChange={(e) => setPastedText(e.target.value)}
                />
              </div>
            )}
          </div>

          {/* Card 2: Action & Model Options */}
          <div className="card">
            <h2 className="card-title">
              <Layers size={20} color="#3b82f6" /> 2. Generation Settings
            </h2>

            {/* Actions Grid */}
            <div className="form-group">
              <label className="form-label">
                Select Learning Artifacts / Actions
                <span className="selection-count">{actions.length} selected</span>
              </label>
              <p className="selection-help">Every selected artifact will be generated for every selected file.</p>
              <div className="actions-grid">
                {configMeta?.actions ? (
                  Object.entries(configMeta.actions).map(([actKey, actInfo]) => (
                    <div
                      key={actKey}
                      className={`action-card ${actions.includes(actKey) ? 'selected' : ''}`}
                      onClick={() => handleActionChange(actKey)}
                      role="checkbox"
                      aria-checked={actions.includes(actKey)}
                      tabIndex={0}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          handleActionChange(actKey);
                        }
                      }}
                    >
                      <div className="action-card-title">
                        <span className="action-check">{actions.includes(actKey) ? <Check size={13} /> : null}</span>
                        {actInfo.title}
                      </div>
                      <span className="action-card-ext">.{actInfo.default_ext}</span>
                    </div>
                  ))
                ) : (
                  <div style={{ color: '#94a3b8', fontSize: '0.85rem' }}>Loading actions...</div>
                )}
              </div>
            </div>

            {/* Provider and Model */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }} className="form-group">
              <div>
                <label className="form-label">LLM Connector</label>
                <select
                  className="form-select"
                  value={connector}
                  onChange={(e) => handleConnectorChange(e.target.value)}
                >
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic (Claude)</option>
                  <option value="openrouter">OpenRouter</option>
                  <option value="ollama">Ollama (Local)</option>
                </select>
              </div>

              <div>
                <label className="form-label">Model Override</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="Default provider model"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>
            </div>

            {/* Output Format */}
            <div className="form-group">
              <label className="form-label">Output Extension / Format Override</label>
              <input
                type="text"
                className="form-input"
                placeholder="Leave blank to use each artifact's default"
                value={outputFormat}
                onChange={(e) => setOutputFormat(e.target.value)}
              />
            </div>

            {/* Optional API Key */}
            {connector !== 'ollama' && (
              <div className="form-group">
                <label className="form-label">API Key Override (Optional)</label>
                <input
                  type="password"
                  className="form-input"
                  placeholder="Leave blank to use server environment variable"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </div>
            )}

            {/* Collapsible Advanced Settings */}
            <div>
              <div
                className="collapsible-header"
                onClick={() => setShowAdvanced(!showAdvanced)}
              >
                <span style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                  <Settings size={16} /> Advanced Pipeline Controls
                </span>
                {showAdvanced ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
              </div>

              {showAdvanced && (
                <div className="collapsible-body">
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                    <div className="form-group">
                      <label className="form-label">Chunk Strategy</label>
                      <select
                        className="form-select"
                        value={strategy}
                        onChange={(e) => setStrategy(e.target.value)}
                      >
                        <option value="">Default (Semantic)</option>
                        <option value="baseline">Baseline</option>
                        <option value="chunked">Chunked</option>
                        <option value="multi_pass">Multi-Pass</option>
                      </select>
                    </div>

                    <div className="form-group">
                      <label className="form-label">Chunk Tokens</label>
                      <input
                        type="number"
                        className="form-input"
                        placeholder="e.g. 5000"
                        value={chunkTokens}
                        onChange={(e) => setChunkTokens(e.target.value)}
                      />
                    </div>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Temperature (0.0 - 1.0)</label>
                    <input
                      type="number"
                      step="0.1"
                      min="0"
                      max="1"
                      className="form-input"
                      placeholder="e.g. 0.2"
                      value={temperature}
                      onChange={(e) => setTemperature(e.target.value)}
                    />
                  </div>
                </div>
              )}
            </div>

            {/* Convert Button */}
            <div style={{ marginTop: '1.5rem' }}>
              <button
                type="button"
                className="btn-primary"
                disabled={loading || backendStatus === 'offline'}
                onClick={handleConvert}
              >
                {loading ? (
                  <>
                    <RefreshCw size={20} className="spinner" />
                    Queueing {batchProgress.completed} of {batchProgress.total}...
                  </>
                ) : (
                  <>
                    <Sparkles size={20} />
                    Start {plannedConversions || ''} Conversion{plannedConversions === 1 ? '' : 's'}
                  </>
                )}
              </button>
            </div>
          </div>
        </div>

        {/* Right Column: Results & Retrieval */}
        <div>
          <div className="card" style={{ minHeight: '100%' }}>
            <div className="results-header">
              <h2 className="card-title" style={{ margin: 0 }}>
                <Folder size={20} color="#10b981" /> Generated Outputs
              </h2>
              {results.length > 0 && <span className="results-count">{results.length} completed</span>}
            </div>

            {results.length > 0 ? (
              <div className="results-list">
                {results.map((result) => (
                  <article className="result-card" key={result.session_id}>
                    <div className="result-card-header">
                      <div>
                        <h3>{result.input_filename}</h3>
                        <span>{result.action_title}</span>
                      </div>
                      <div className="result-actions">
                        <button type="button" className="btn-secondary" onClick={() => handleCopy(result)}>
                          {copiedId === result.session_id ? <Check size={16} color="#10b981" /> : <Copy size={16} />}
                          {copiedId === result.session_id ? 'Copied' : 'Copy'}
                        </button>
                        <a
                          href={result.download_url}
                          download={result.output_filename}
                          className="btn-secondary btn-download"
                          style={{ textDecoration: 'none' }}
                        >
                          <Download size={16} /> Download
                        </a>
                      </div>
                    </div>

                    <div className="output-container">{result.output_text}</div>

                    <div className="output-meta-row">
                      <span>File: <strong>{result.output_filename}</strong></span>
                      {result.metrics?.runtime_seconds != null && (
                        <span>Runtime: <strong>{result.metrics.runtime_seconds.toFixed(2)}s</strong></span>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div style={{
                textAlign: 'center',
                padding: '4rem 2rem',
                color: '#64748b'
              }}>
                <FileText size={48} style={{ margin: '0 auto 1rem', opacity: 0.4 }} />
                <p style={{ fontWeight: 500, color: '#94a3b8' }}>No output generated yet</p>
                <p style={{ fontSize: '0.85rem', marginTop: '0.25rem' }}>
                  Select one or more files and artifacts on the left, then start the conversion batch.
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
        </>
      )}
    </div>
  );
}
