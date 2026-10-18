import { render } from '/plugins/trackside_core/static/trackside.js';

export default async function (shadow, ctx) {
  await render(shadow, ctx, 'plate');
}
