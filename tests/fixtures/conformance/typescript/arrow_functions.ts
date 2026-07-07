// Feature: const-assigned arrow functions are symbols; calls between them resolve.

export const double = (x: number): number => x * 2;

export const quadruple = (x: number): number => double(double(x));

const log = (msg: string): void => {
  console.log(msg);
};
