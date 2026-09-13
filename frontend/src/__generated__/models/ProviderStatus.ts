/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { ProviderJob } from "./ProviderJob";
export type ProviderStatus = {
  enabled: boolean;
  torrent_configured: boolean;
  index_ready: boolean;
  records?: number;
  indexed_at?: string | null;
  platforms?: Array<string>;
  jobs?: Array<ProviderJob>;
};
