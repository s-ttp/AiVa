import { describe, expect, it } from 'vitest';
import { readRuntimeConfig } from './config';

describe('readRuntimeConfig', () => {
  it.each([
    [{ gatewayUrl: 'http://localhost:8000' }, 'http://localhost:8000'],
    [{ gatewayUrl: ' https://gateway.example/api/ ' }, 'https://gateway.example/api'],
    [{ gatewayUrl: '' }, null],
    [{ gatewayUrl: 'javascript:alert(1)' }, null],
    [{ gatewayUrl: 42 }, null],
    [null, null],
    [{}, null],
  ])('reads %j as %s', (raw, expected) => {
    expect(readRuntimeConfig(raw)).toEqual({ gatewayUrl: expected, storyMode: false });
  });

  it.each([
    [{ storyMode: true }, true],
    [{ storyMode: 'true' }, false],
    [{ storyMode: 1 }, false],
    [{ storyMode: false }, false],
  ])('turns story mode on only for a literal true: %j', (raw, expected) => {
    expect(readRuntimeConfig(raw).storyMode).toBe(expected);
  });
});
