export function webRead(input: string, signal?: AbortSignal): Promise<{
  markdown?: string;
  image?: { type: "image"; data: string; mimeType: string };
  finalUrl: string;
  contentType: string;
}>;
