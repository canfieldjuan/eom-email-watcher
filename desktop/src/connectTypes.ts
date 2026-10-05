export interface ConnectProviderIdentity {
  app_id: string;
  version: string;
  instance_id: string;
}

export interface ConnectProvider extends ConnectProviderIdentity {
  name: string;
}

export interface ConnectCapabilityRef {
  id: string;
  version: string;
}

export interface ConnectParameter {
  name: string;
  value_type: "string" | "integer" | "boolean";
  required: boolean;
  label: string;
  description: string;
}

export interface ConnectCapabilityDeclaration extends ConnectCapabilityRef {
  action: { label: string; description: string };
  accepts: { media_type: string; max_bytes: number }[];
  produces: string[];
  parameters: ConnectParameter[];
  effects: { external: boolean; confirmation_required: boolean };
}

export interface ConnectCapability {
  protocol_version: 2;
  provider: ConnectProvider;
  capability: ConnectCapabilityDeclaration;
}

export interface ConnectOutputMetadata {
  artifact_id: string;
  media_type: string;
  display_name: string;
  byte_size: number;
  sha256: string;
}

export interface ConnectCapabilities {
  items: ConnectCapability[];
  diagnostic: { code: string } | null;
}
