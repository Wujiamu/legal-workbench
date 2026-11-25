export const EXPORT_SOFTWARE_NAME = 'PlainFolio (素章)';

function escapeMetaXml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

export function buildSvgMetadata(title: string): string {
  return '<metadata>'
    + '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    + 'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    + '<rdf:Description>'
    + `<dc:title>${escapeMetaXml(title)}</dc:title>`
    + `<dc:creator>${EXPORT_SOFTWARE_NAME}</dc:creator>`
    + '</rdf:Description>'
    + '</rdf:RDF>'
    + '</metadata>';
}
