export interface SourceRecord {
  record_id: string;
  source: "NSP" | "SOPC" | "EARLY_WARNING";
  title: string;
  document_text: string;
  metadata: Record<string, string | number | boolean | null>;
  distance: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  sources?: SourceRecord[];
  retrievalMs?: number;
  route?: string;
  dataset?: string;
  sql?: string;
  streaming?: boolean;
  error?: boolean;
}

export interface HealthStatus {
  status: string;
  mode: string;
  gemini_configured: boolean;
  nsp_record_count: number;
  sopc_record_count: number;
  high_risk_count: number;
  indexed_record_count: number;
  indexed: { nsp: number; sopc: number };
  dropout_model_ready: boolean;
}

export interface DropoutAlert {
  athlete_id: string;
  sport: string;
  federation: string;
  region: string;
  city: string;
  age: number | null;
  age_group: string;
  dropout_probability: number;
  risk_level: "HIGH" | "MEDIUM" | "LOW";
  days_since_activity: number;
  signals: string[];
  recommended_action: string;
}

export interface DropoutResponse {
  ready: boolean;
  summary: {
    total_monitored?: number;
    high_risk?: number;
    medium_risk?: number;
    average_risk_pct?: number;
    snapshot_date?: string;
  };
  alerts: DropoutAlert[];
}
