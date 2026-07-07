// Feature: interface and type-alias declarations are first-class symbols.

export interface User {
  id: number;
  name: string;
}

export type UserId = number;

export type Handler = (u: User) => void;

export function register(user: User): UserId {
  return user.id;
}
