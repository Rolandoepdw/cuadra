const KEY = "cuadra.pin";

export function getPin(): string {
  return localStorage.getItem(KEY) ?? "";
}

export function setPin(pin: string): void {
  localStorage.setItem(KEY, pin);
}

export function clearPin(): void {
  localStorage.removeItem(KEY);
}

export function hasPin(): boolean {
  return getPin().length > 0;
}
