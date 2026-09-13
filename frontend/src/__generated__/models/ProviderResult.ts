/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { ProviderOption } from "./ProviderOption";
export type ProviderResult = {
  id: string;
  provider: "minerva" | "axekin" | "vimm" | "edgeemu" | "startgame";
  name: string;
  platform?: string;
  region?: string;
  collection?: string;
  filename?: string;
  size?: number | null;
  source_url: string;
  options?: Array<ProviderOption>;
};
