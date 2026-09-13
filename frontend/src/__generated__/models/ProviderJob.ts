/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export type ProviderJob = {
  id: string;
  kind: string;
  state: string;
  phase?: string;
  name?: string;
  progress?: number;
  completed_bytes?: number;
  total_bytes?: number | null;
  records?: number;
  error?: string | null;
  rom_id?: number | null;
};
