package demo;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class CheckoutTest {
 @Test void validCoupon() {
  assertEquals(9000, new CheckoutResource().checkout(new CheckoutResource.Order(10000,"SAVE10")).totalCents());
 }
}
