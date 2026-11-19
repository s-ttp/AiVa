import type {
  AgentWorkflowRequest,
  ArtifactCreateRequest,
  Classification,
  Scenario,
} from '../api/client';
import { DEFAULT_SIMULATED_IP } from '../run/draft';

/** Catalog scenarios these views run (LLD §15). */
export const DOCUMENT_SCENARIO_ID = 'document-copilot';
export const AGENT_SCENARIO_ID = 'agentic-workflow';

export interface EvidenceDraft {
  id: string;
  text: string;
}

export interface FigureDraft {
  id: string;
  label: string;
  value: string;
  unit: string;
  sourceId: string;
}

export interface ArtifactDraft {
  scenarioId: string;
  applicationId: string;
  simulatedSourceIp: string;
  documentType: 'docx' | 'pptx';
  title: string;
  instructions: string;
  classification: Classification | '';
  evidence: EvidenceDraft[];
  figures: FigureDraft[];
  maxOutputTokens: number;
}

export const emptyEvidence = (): EvidenceDraft => ({ id: '', text: '' });
export const emptyFigure = (): FigureDraft => ({ id: '', label: '', value: '', unit: '', sourceId: '' });

/**
 * A new document draft from the scenario: its application, classification and
 * the catalog's synthetic sample request (evidence and critical figures
 * included). Without a sample the evidence is the tester's input; the UI
 * never invents source content.
 */
export function artifactDraftFor(scenario: Scenario | undefined): ArtifactDraft {
  const sample = scenario?.sample_artifact;
  return {
    scenarioId: scenario?.id ?? '',
    applicationId: scenario?.application_id ?? '',
    simulatedSourceIp: DEFAULT_SIMULATED_IP,
    documentType: sample?.document_type ?? 'pptx',
    title: sample?.title ?? '',
    instructions: sample?.instructions ?? scenario?.sample_prompt ?? '',
    classification: scenario?.declared_classification ?? '',
    evidence: sample?.evidence.length
      ? sample.evidence.map((item) => ({ id: item.id, text: item.text }))
      : [emptyEvidence()],
    figures: (sample?.figures ?? []).map((figure) => ({
      id: figure.id,
      label: figure.label,
      value: figure.value,
      unit: figure.unit ?? '',
      sourceId: figure.source_id,
    })),
    maxOutputTokens: 2048,
  };
}

/** The POST /v1/artifacts body: traced, as a simulated Test Console identity. Blank rows are dropped. */
export function toArtifactRequest(draft: ArtifactDraft): ArtifactCreateRequest {
  return {
    document_type: draft.documentType,
    title: draft.title.trim(),
    instructions: draft.instructions.trim(),
    evidence: draft.evidence
      .filter((item) => item.id.trim() || item.text.trim())
      .map((item) => ({ id: item.id.trim(), text: item.text })),
    figures: draft.figures
      .filter((figure) => figure.id.trim() || figure.value.trim())
      .map((figure) => ({
        id: figure.id.trim(),
        label: figure.label.trim(),
        value: figure.value.trim(),
        unit: figure.unit.trim() || null,
        source_id: figure.sourceId.trim(),
      })),
    data_classification: draft.classification || null,
    scenario_id: draft.scenarioId || null,
    max_output_tokens: draft.maxOutputTokens,
    trace: true,
    test_profile: {
      application_id: draft.applicationId,
      simulated_source_ip: draft.simulatedSourceIp.trim() || null,
    },
  };
}

export interface WorkflowDraft {
  scenarioId: string;
  applicationId: string;
  simulatedSourceIp: string;
  incidentId: string;
  instructions: string;
  classification: Classification | '';
  qualityTier: 'standard' | 'high' | 'critical';
  maxOutputTokens: number;
}

export function workflowDraftFor(scenario: Scenario | undefined): WorkflowDraft {
  return {
    scenarioId: scenario?.id ?? '',
    applicationId: scenario?.application_id ?? '',
    simulatedSourceIp: DEFAULT_SIMULATED_IP,
    incidentId: scenario?.sample_workflow?.incident_id ?? '',
    instructions: scenario?.sample_workflow?.instructions ?? scenario?.sample_prompt ?? '',
    classification: scenario?.declared_classification ?? '',
    qualityTier: 'standard',
    // Each step's cap. Gemini Flash-Lite's analysis step ran to 638 tokens live (ADR-0009, 2026-10-22).
    maxOutputTokens: 1024,
  };
}

/** The POST /v1/agents/workflows body, as a simulated Test Console identity. */
export function toWorkflowRequest(draft: WorkflowDraft): AgentWorkflowRequest {
  return {
    agent: 'service-incident',
    incident_id: draft.incidentId.trim(),
    instructions: draft.instructions.trim() || null,
    data_classification: draft.classification || null,
    quality_tier: draft.qualityTier,
    max_output_tokens: draft.maxOutputTokens,
    scenario_id: draft.scenarioId || null,
    test_profile: {
      application_id: draft.applicationId,
      simulated_source_ip: draft.simulatedSourceIp.trim() || null,
    },
  };
}
